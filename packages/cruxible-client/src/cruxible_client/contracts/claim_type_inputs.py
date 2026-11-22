"""The authored ClaimType input and its lowering: the short form's defaults.

One ClaimType input -- state what differs from the defaults -- and the one
function that lowers it to a whole ClaimType. The daemon's
``claim-type propose`` route, ``claim-type migrate`` and the SDK's
``ChangeSetDraft.field`` all lower through :func:`lower_claim_type_input`, so a
field defined on any surface takes the same defaults and has the same digest.
Pure: it reads nothing but the predecessor (and, for digest-named rules, the
tree) it is given.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal, cast

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    StrictBool,
    StrictStr,
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
    parse_artifact_identity,
)
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.canonical_forms import CanonicalTransform
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CONTRACT_ID,
    capture_contract_digest,
    parse_capture_contract,
)
from cruxible_client.contracts.claim_type_structure import ClaimRole
from cruxible_client.contracts.claim_types import (
    ClaimAttestationConsequencePolicy,
    ClaimFreshnessDuration,
    ClaimInverse,
    ClaimInverseKey,
    ClaimPrecedence,
    ClaimType,
    EvidenceRequirement,
    RevisionEvidence,
    canonical_description_text,
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
)
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.policies import (
    CAPTURE_CONTRACT_REF_ROLE,
    PER_FILE_EVIDENCE_RULE_REPAIR,
    CaptureContractAdmission,
    ClaimEvidenceAdmissionPolicy,
    added_per_file_contracts,
)
from cruxible_client.contracts.public_names import public_name, stored_contract_name
from cruxible_client.contracts.shapes import (
    FieldType,
    StringType,
    coerce_field_type,
    value_members,
)
from cruxible_client.contracts.temporal import parse_iso_duration_microseconds


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


_MERGED_INPUT_FIELDS = (
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
    "value",
    "precedence",
    "requires_complete_input",
    "incomplete_fallback",
    "allowed_subject_kinds",
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

#: The predicate is required by the model; cardinality has no inferred default.
_REQUIRED_NEW_FIELDS = ("cardinality",)

#: Fields that always have a value: named or left out, never ``null``.
_NEVER_NULL_FIELDS = (
    "allowed_subject_kinds",
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


#: What each modelling and policy lint means and how to fix it, one line each:
#: every surface prints a lint's code with this text.
CLAIM_TYPE_LINT_TEXT: dict[str, str] = {
    "cruxible.claim_type.description_missing": "state what one value of this field means",
    "cruxible.claim_type.field_restates_ledger": (
        "the ledger already records who wrote and approved each value, when, why and what "
        "was proposed: drop the field and read those from the Claim"
    ),
    "cruxible.claim_type.string_holds_prose": (
        "a long free string holds prose: keep strings to identifiers and one short label, "
        "and put prose in a Document section the Claim cites"
    ),
    "cruxible.claim_type.enum_members_name_things": (
        "members that look like versions, dates or identifiers name things with their own "
        "facts: make the thing a kind and the field subject<K>"
    ),
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


class ClaimInverseInput(_StrictClaimTypeInputModel):
    """How the type states that a unit holds nothing.

    ``key`` names the unit: ``{kind: slot}`` for a one-valued type,
    ``{kind: object}`` or ``{kind: object_key, predicate}`` (a key ClaimType of
    every object SubjectKind) for a many-valued one. ``evidence_rules`` are ids
    of the type's own evidence rules, in any order; only a Capture one of them
    admits establishes a none. ``stale_after`` takes ``P7D`` (absent: the type's
    evidence freshness applies). ``tag`` may be left out; it is accepted so a
    stored ClaimType's own inverse is also its input form.
    """

    tag: Literal["cruxible-claim-inverse-v1"] = "cruxible-claim-inverse-v1"
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

    A new ClaimType states ``predicate`` and ``cardinality``. The predicate's
    prefix before its last dot supplies ``allowed_subject_kinds``; state it for
    a multi-kind field. ``value`` declares a native Contract field type;
    omitted, it is ``string{max_len: 256}``. Subject-valued fields state
    ``subject<K>`` or ``subject<K1 | K2>``. Prose belongs in Documents.
    Every other field takes its default, written out in full in the stored ClaimType
    (``cruxible claim-type propose --template --expanded`` prints them):

    - ``permitted_roles``: ``[default_role]`` when one is stated, else
      ``[normative]``.
    - ``evidence_admission_policy``: with ``evidence_requirement`` ``self`` (the
      default) or ``none``, one own-words rule (``self``, contract
      ``cruxible.coordinator-self-source-v1``) for every permitted role but
      ``derivation``, which also admits workspace file and section citations;
      plus, for ``evidence: [<contract>, ...]``, one rule admitting those shaped
      contracts (derivationally for ``derivation``); with ``captured``, the
      ``evidence`` rules only.
    - ``admission_policy``: no corroboration or freeze requirements.
    - ``resolution_policy``: supported, current Claims; ``only_contender``
      (one) or ``all`` (many); a conflict leaves the field unresolved.
    - ``evidence_requirement``: ``captured`` for exactly ``[observation]``
      (which requires a source or explicit evidence policy), otherwise ``self``.
      ``revision_evidence``: ``replace``.
    - ``canonical_form``: ``[nfc, trim]`` for string literals, enum included;
      state ``[]`` for no transforms. Other object types have no default form.

    An edit (a predicate this instance already holds) follows JSON merge-patch
    (RFC 7396) against the accepted predecessor for every field but
    ``predicate``: a field left out keeps the predecessor's value, so an
    unrelated edit never drops what the type means or how it is backed. An
    explicit ``null`` clears a field that may be empty (``evidence_freshness``,
    ``attestation_consequence_policy``,
    ``unsure_hold_for``, ``description``, ``member_descriptions`` (``[]`` too),
    ``default_role``, ``canonical_form``, ``inverse``); the others always have a value, so
    they are named or left out, never ``null``. Durations (``evidence_freshness.stale_after``,
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

    value: FieldType | None = None
    precedence: ClaimPrecedence = ClaimPrecedence()
    requires_complete_input: tuple[StrictStr | StrictBool, ...] = ()
    incomplete_fallback: StrictStr | StrictBool | None = None

    @model_validator(mode="before")
    @classmethod
    def _retired(cls, value: object) -> object:
        if isinstance(value, dict) and value.get("object_kind") == "exact_content":
            raise ValueError(
                "cruxible.claim.exact_content_retired: prose lives in Documents; "
                "cite a Document section"
            )
        if isinstance(value, dict) and any(
            name in value
            for name in ("object_kind", "literal_schema", "allowed_object_subject_kinds")
        ):
            raise ValueError(
                "cruxible.claim_type.retired_schema: declare value with the Contract type grammar"
            )
        return value

    @field_validator("value", mode="before")
    @classmethod
    def _value_type(cls, value: Any) -> object:
        return None if value is None else coerce_field_type(value)

    predicate: str
    allowed_subject_kinds: tuple[str, ...] | None = None
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
    #: What the predicate means, NFC-normalized and trimmed.
    #: Omitted: kept from the predecessor. ``null``: cleared.
    description: str | None = None
    #: What each literal enum member means, in any order.
    #: Omitted: kept from the predecessor. ``null`` or ``[]``: cleared.
    member_descriptions: tuple[ClaimTypeMemberDescriptionInput, ...] | None = None
    #: The role a write takes when it names none.
    #: Omitted: kept from the predecessor. ``null``: cleared.
    default_role: ClaimRole | None = None
    #: Omitted: kept from the predecessor; a new ClaimType takes ``self``.
    #: Never ``null``.
    evidence_requirement: EvidenceRequirement | None = None
    #: Omitted: kept from the predecessor; a new ClaimType takes ``replace``.
    #: Never ``null``.
    revision_evidence: RevisionEvidence | None = None
    #: The ordered transforms every literal value is stored and compared in
    #: (``contracts.canonical_forms``). Omitted: kept from the predecessor (none
    #: for a new ClaimType). ``null`` or ``[]``: no transform.
    canonical_form: tuple[CanonicalTransform, ...] | None = None
    #: How the type states absence. Omitted: kept from the predecessor (none
    #: for a new ClaimType). ``null``: cleared.
    inverse: ClaimInverseInput | None = None
    #: The shaped CaptureContracts whose Captures back this field, by name: the
    #: short form of ``evidence_admission_policy``. Each is admitted for the
    #: field's permitted roles (derivationally for ``derivation``). Own words,
    #: and workspace file and section citations, need no entry: they are
    #: admitted on every field that is not ``evidence_requirement: captured``.
    evidence: tuple[str, ...] | None = None
    #: Retired authoring field: refused with a repair naming ``evidence``.
    anticipated_source_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _stated_semantics(self) -> "ClaimTypeInputRecord":
        for field in _NEVER_NULL_FIELDS:
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null: name a value or leave it out")
        if self.evidence is not None and self.evidence_admission_policy is not None:
            raise ValueError(
                "state evidence (the shaped contracts) or evidence_admission_policy (the "
                "full rules), not both"
            )
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

    @field_validator("evidence")
    @classmethod
    def _evidence(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("evidence lists at least one shaped CaptureContract; leave it out")
        if len(set(value)) != len(value):
            raise ValueError("evidence names each contract once")
        return value


class ClaimTypeInputIncomplete(FormatError):
    """A new ClaimType leaves out a field no default can fill."""

    error_code = "cruxible.claim_type.input_incomplete"


class ClaimTypePerFileEvidenceRule(FormatError):
    """A ClaimType's evidence rule newly names one workspace file's own contract."""

    error_code = "cruxible.claim_type.per_file_evidence_rule"


def refuse_added_per_file_rules(
    predicate: str,
    policy: ClaimEvidenceAdmissionPolicy,
    predecessor: ClaimEvidenceAdmissionPolicy | None,
) -> None:
    """Refuse a rule that newly names a per-file foreign-source contract.

    A rule the accepted ClaimType already holds is left as it is, so the field
    stays editable; only a newly named per-file contract refuses.
    """

    added = added_per_file_contracts(policy, predecessor)
    if not added:
        return
    index, name = added[0]
    rule_id = policy.rules[index].rule_id
    raise ClaimTypePerFileEvidenceRule(
        f"{ClaimTypePerFileEvidenceRule.error_code}: ClaimType:{predicate} evidence rule "
        f"{rule_id!r} names CaptureContract:{public_name(name)}; {PER_FILE_EVIDENCE_RULE_REPAIR}"
    )


#: The repair a retired source list names: shaped contracts go in ``evidence``.
SOURCES_RETIRED_REPAIR = (
    "a workspace file or section citation needs no rule (it is admitted wherever own "
    "words are); for a particular kind of source, list its shaped CaptureContract in "
    "evidence: [<contract name>, ...]"
)


class ClaimTypeSourcesRetired(FormatError):
    """``anticipated_source_ids`` (the SDK's ``sources=``) is no longer authored."""

    error_code = "cruxible.claim_type.sources_retired"

    def __init__(self, spelling: str = "anticipated_source_ids") -> None:
        from cruxible_client.contracts.repairs import HandEditInstruction, HandEditRepair

        #: Carried to every surface: the HTTP envelope renders it as the repair.
        self.repair = HandEditRepair(
            hand_edit=HandEditInstruction(
                target=spelling, required_change=f"Drop {spelling}: {SOURCES_RETIRED_REPAIR}"
            )
        )
        super().__init__(f"{self.error_code}: {spelling} is retired: {SOURCES_RETIRED_REPAIR}")


def refuse_retired_sources(value: "ClaimTypeInputRecord") -> None:
    if value.anticipated_source_ids:
        raise ClaimTypeSourcesRetired()


class ClaimTypeFamilyRuleNotAuthored(FormatError):
    """An authored rule newly names a capture-contract family."""

    error_code = "cruxible.claim_type.family_rule_not_authored"


WORKSPACE_FILE_FAMILY = "workspace_file"


def refuse_added_family_rules(
    predicate: str,
    policy: ClaimEvidenceAdmissionPolicy,
    predecessor: ClaimEvidenceAdmissionPolicy | None,
) -> None:
    """Workspace citations need no rule: refuse a family a rule newly names on its own.

    A family beside the own-words contract is what lowering writes itself, and
    a rule the accepted ClaimType already holds stays as it is.
    """

    held = {
        rule.rule_id
        for rule in (() if predecessor is None else predecessor.rules)
        if rule.capture_contract_families
    }
    for rule in policy.rules:
        own_words = any(
            item.target.name == COORDINATOR_SELF_SOURCE_CONTRACT_ID
            for item in rule.capture_contracts
        )
        if rule.capture_contract_families and not own_words and rule.rule_id not in held:
            raise ClaimTypeFamilyRuleNotAuthored(
                f"{ClaimTypeFamilyRuleNotAuthored.error_code}: ClaimType:{predicate} evidence "
                f"rule {rule.rule_id!r} names capture_contract_families; a workspace file or "
                "section citation needs no rule: it is admitted wherever own words are. For "
                "a particular kind of source, list its shaped CaptureContract in "
                "evidence: [<contract name>, ...]"
            )


class ClaimTypeEvidenceRulesRequired(FormatError):
    """A captured-only ClaimType names no source its evidence could come from."""

    error_code = "cruxible.claim_type.evidence_rules_required"


#: Both repairs for a captured-only field with no source: the contract one and
#: the cheap one (a hand-cited value is normative and needs no contract).
_EVIDENCE_RULES_REPAIR = (
    "Repair: list its shaped CaptureContracts in evidence: [<contract name>, ...]; "
    "or, for a value cited by hand from a workspace file, permitted_roles: [normative] "
    "(a file citation needs no contract)"
)


def _admitted_roles(roles: tuple[str, ...]) -> list[str]:
    return sorted({role for role in roles if role != "derivation"}, key=lambda r: r.encode())


def default_evidence_policy(
    roles: tuple[str, ...], requirement: str, evidence: tuple[str, ...] = ()
) -> dict[str, object]:
    """The rules a field takes from its roles, requirement and ``evidence`` list.

    Names are in public spelling. Unless the field requires captured evidence,
    one ``self`` rule admits own words and workspace file and section citations;
    ``evidence`` adds one ``evidence`` rule for the permitted roles, and one
    derivational ``derivation`` rule when ``derivation`` is permitted.
    """

    claim_roles = _admitted_roles(roles)
    rules: list[dict[str, object]] = []
    if requirement != "captured" and claim_roles:
        rules.append(
            {
                "rule_id": "self",
                "claim_roles": claim_roles,
                "capture_contracts": [public_name(COORDINATOR_SELF_SOURCE_CONTRACT_ID)],
                "capture_contract_families": [WORKSPACE_FILE_FAMILY],
                "admission": "direct",
                "subject_binding": "exact_claim_subject",
            }
        )
    shaped = sorted(set(evidence), key=lambda item: item.encode("utf-8"))
    if shaped and claim_roles:
        rules.append(
            {
                "rule_id": "evidence",
                "claim_roles": claim_roles,
                "capture_contracts": shaped,
                "admission": "direct",
                "subject_binding": "exact_claim_subject",
            }
        )
    if shaped and "derivation" in roles:
        rules.append(
            {
                "rule_id": "derivation",
                "claim_roles": ["derivation"],
                "capture_contracts": shaped,
                "admission": "derivational",
                "subject_binding": "exact_claim_subject",
            }
        )
    if requirement == "captured" and not rules:
        raise ClaimTypeEvidenceRulesRequired(
            f"{ClaimTypeEvidenceRulesRequired.error_code}: evidence_requirement captured "
            "admits only a shaped payload under a declared contract, and this ClaimType "
            f"names none. {_EVIDENCE_RULES_REPAIR}"
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


def default_resolution_policy(cardinality: Literal["one", "many"]) -> dict[str, object]:
    """The resolution policy a new ClaimType of this cardinality takes when it states none."""

    return {
        "cardinality": cardinality,
        "eligible_verdicts": ["supported"],
        "required_basis_kinds": [],
        "require_current": True,
        "selector": "all" if cardinality == "many" else "only_contender",
        "conflict_result": "unresolved",
    }


def claim_type_input_defaults(
    value: ClaimTypeInputRecord, predecessor: ClaimType | None
) -> ClaimTypeInputRecord:
    """The same input with every structural field stated.

    Over a predecessor, a field left out takes the predecessor's value
    (merge-patch); over none, it takes its default, and a field no default can
    fill refuses ``cruxible.claim_type.input_incomplete``. A minimal input and
    its spelled-out equivalent therefore lower to one ClaimType and one digest.
    The description, role, canonical-form and inverse fields are merged later,
    by lowering; a new ClaimType states its evidence requirement and revision
    evidence here so the expanded form shows them.
    """

    refuse_retired_sources(value)
    data = value.model_dump(mode="json")
    stated = value.model_fields_set
    if predecessor is not None:
        prior = predecessor.model_dump(mode="json")
        for field in _STRUCTURAL_INPUT_FIELDS:
            if field not in stated and field in prior:
                data[field] = prior[field]
        if value.evidence is not None:
            data["evidence_admission_policy"] = default_evidence_policy(
                tuple(cast(list[str], data["permitted_roles"])),
                str(value.evidence_requirement or prior["evidence_requirement"]),
                value.evidence,
            )
            data.pop("evidence", None)
        elif "evidence_admission_policy" not in stated:
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
    if "allowed_subject_kinds" not in stated:
        data["allowed_subject_kinds"] = [value.predicate.rpartition(".")[0]]
    if "value" not in stated:
        data["value"] = StringType(max_len=256).model_dump(mode="json")
    if "canonical_form" not in stated and coerce_field_type(data["value"]).kind in {
        "string",
        "enum",
        "enum_of",
    }:
        data["canonical_form"] = ["nfc", "trim"]
    if "permitted_roles" not in stated:
        data["permitted_roles"] = [value.default_role or "normative"]
    requirement = value.evidence_requirement or (
        "captured" if set(data["permitted_roles"]) == {"observation"} else "self"
    )
    if "evidence_requirement" not in stated:
        data["evidence_requirement"] = requirement
    if "revision_evidence" not in stated:
        data["revision_evidence"] = "replace"
    if "evidence_admission_policy" not in stated:
        data["evidence_admission_policy"] = default_evidence_policy(
            tuple(data["permitted_roles"]),
            requirement,
            value.evidence or (),
        )
    data.pop("evidence", None)
    if "admission_policy" not in stated:
        data["admission_policy"] = {"corroboration_requirements": [], "freeze_requirements": []}
    if "resolution_policy" not in stated:
        data["resolution_policy"] = default_resolution_policy(
            cast(Literal["one", "many"], value.cardinality)
        )
    return ClaimTypeInputRecord.model_validate(data)


def claim_type_input_template(*, expanded: bool = False) -> ClaimTypeInputRecord:
    """The ClaimType input the CLI template surface prints.

    The minimal form states only predicate and cardinality. Its string literal
    takes ``[nfc, trim]`` and can key a SubjectKind.
    ``expanded`` states every default from the same function lowering applies.
    """

    minimal = ClaimTypeInputRecord(
        predicate="project.work_item.status",
        cardinality="one",
    )
    return claim_type_input_defaults(minimal, None) if expanded else minimal


def _contract_identities(tree: Mapping[str, bytes]) -> dict[str, str]:
    """Accepted contract digests to their identities."""

    identities: dict[str, str] = {}
    for path in tree:
        if path.startswith("capture-contracts/") and path.endswith(".json"):
            contract = parse_capture_contract(tree[path], path=path)
            identities[capture_contract_digest(contract).tagged] = contract.identity.qualified
    return identities


def _contract_ref(value: object) -> CaptureContractAdmission:
    # A rule names a contract by its public name or its stored identity; both
    # map to the stored identity, which is what evidence is captured under.
    if isinstance(value, dict):
        ref = CaptureContractAdmission.model_validate(value)
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
    return CaptureContractAdmission(role=CAPTURE_CONTRACT_REF_ROLE, target=identity)


def identity_evidence_policy(
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
        refs = []
        for item in rule.pop("capture_contracts", []) or []:
            if isinstance(item, dict) and "contract" in item:
                ref = _contract_ref(item["contract"])
                refs.append(
                    CaptureContractAdmission.model_validate(
                        {
                            **ref.model_dump(mode="python"),
                            "logical_sources": tuple(item.get("logical_sources", ())),
                        }
                    )
                )
            else:
                ref = _contract_ref(item)
                refs.append(ref)
        for digest in rule.pop("capture_contract_digests", []) or []:
            identity = identities.get(digest) if isinstance(digest, str) else None
            if identity is None:
                raise ClaimTypeInputReferenceError(
                    f"capture contract digest {digest!r} is not an accepted CaptureContract; "
                    "name the contract in capture_contracts instead"
                )
            refs.append(
                CaptureContractAdmission.model_validate(
                    _contract_ref(identity).model_dump(mode="python")
                )
            )
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


class ClaimTypeMemberDescriptionsStale(FormatError):
    """Member descriptions name values the edited enum no longer admits."""

    error_code = "cruxible.claim_type.member_descriptions_stale"


class ClaimTypeDefaultRoleNotPermitted(FormatError):
    """The default role is not one of the edited ClaimType's authorable roles."""

    error_code = "cruxible.claim_type.default_role_not_permitted"


def _description_fields(
    value: ClaimTypeInputRecord, predecessor: ClaimType | None
) -> dict[str, object]:
    """The description, role and evidence fields a lowered ClaimType states.

    Merge-patched onto its predecessor: a field the input leaves out keeps the
    predecessor's value, so an unrelated edit never changes what the type
    means, what a Claim needs or what a revision keeps. A new ClaimType takes
    ``self`` and ``replace``. Inherited descriptions and default role are
    checked against the edited structure and refused, never silently dropped,
    when stale.
    """

    if value.states("description"):
        description = (
            None if value.description is None else canonical_description_text(value.description)
        )
    else:
        description = None if predecessor is None else predecessor.description
    if value.states("member_descriptions"):
        members: list[dict[str, object]] = [
            {"member": item.member, "description": canonical_description_text(item.description)}
            for item in value.member_descriptions or ()
        ]
    else:
        members = (
            []
            if predecessor is None
            else [item.model_dump(mode="json") for item in predecessor.member_descriptions]
        )
    members.sort(key=lambda item: canonical_bytes(item["member"]))
    enum = list(value_members(value.value))
    admitted = {canonical_bytes(item) for item in enum} if isinstance(enum, list) else set()
    stale = [item["member"] for item in members if canonical_bytes(item["member"]) not in admitted]
    if stale:
        raise ClaimTypeMemberDescriptionsStale(
            f"{ClaimTypeMemberDescriptionsStale.error_code}: member_descriptions name "
            f"{', '.join(repr(item) for item in stale)}, which the value enum no "
            "longer admits; describe the current members, or pass member_descriptions: null "
            "to clear them"
        )
    if value.states("default_role"):
        default_role = value.default_role
    else:
        default_role = None if predecessor is None else predecessor.default_role
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
        or ("self" if predecessor is None else predecessor.evidence_requirement),
        "revision_evidence": value.revision_evidence
        or ("replace" if predecessor is None else predecessor.revision_evidence),
    }


def _canonical_form(
    value: ClaimTypeInputRecord, predecessor: ClaimType | None
) -> tuple[CanonicalTransform, ...]:
    """The canonical form a lowered ClaimType states.

    Left out, the predecessor's form is kept (merge-patch), so an unrelated edit
    never changes what the predicate's values compare as; ``null`` states no
    transform.
    """

    if value.states("canonical_form"):
        return value.canonical_form or ()
    return () if predecessor is None else predecessor.canonical_form


def _inverse(value: ClaimTypeInputRecord, predecessor: ClaimType | None) -> ClaimInverse | None:
    """The inverse a lowered ClaimType declares: kept when left out, cleared by ``null``."""

    if value.states("inverse"):
        return None if value.inverse is None else value.inverse.lowered()
    return None if predecessor is None else predecessor.inverse


def lower_claim_type_input(
    value: ClaimTypeInputRecord,
    *,
    tree: Mapping[str, bytes],
) -> ClaimType:
    path = claim_type_path(value.predicate)
    predecessor = None
    if path in tree:
        predecessor = parse_claim_type(tree[path], path=path)
    value = claim_type_input_defaults(value, predecessor)
    payload = value.model_dump(mode="json")
    payload.pop("anticipated_source_ids", None)
    payload.pop("evidence", None)
    for field in _MERGED_INPUT_FIELDS:
        payload.pop(field, None)
    payload["canonical_form"] = list(_canonical_form(value, predecessor))
    inverse = _inverse(value, predecessor)
    if inverse is not None:
        payload["inverse"] = inverse.model_dump(mode="json")
    # Every rule names its contracts by identity. A rule naming an exact digest
    # no accepted contract version carries has no identity to follow, so it is
    # refused.
    policy = value.evidence_admission_policy or {}
    raw_rules = policy.get("rules", ())
    needs_digest_lookup = isinstance(raw_rules, list | tuple) and any(
        isinstance(rule, dict) and bool(rule.get("capture_contract_digests")) for rule in raw_rules
    )
    identity_policy = identity_evidence_policy(
        policy,
        identities=(_contract_identities(tree) if needs_digest_lookup else {}),
    )
    payload.update(_description_fields(value, predecessor))
    try:
        evidence_policy = ClaimEvidenceAdmissionPolicy.model_validate(identity_policy)
    except ValidationError as exc:
        raise ClaimTypeInputValidationError(exc) from exc
    payload["evidence_admission_policy"] = evidence_policy.model_dump(mode="json")
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


__all__ = [
    "CLAIM_TYPE_LINT_TEXT",
    "ClaimDurationInput",
    "ClaimEvidenceFreshnessInput",
    "ClaimInverseInput",
    "ClaimTypeDefaultRoleNotPermitted",
    "ClaimTypeEvidenceRulesRequired",
    "ClaimTypeInputIncomplete",
    "ClaimTypeInputRecord",
    "ClaimTypeInputReferenceError",
    "ClaimTypeInputValidationError",
    "ClaimTypeMemberDescriptionInput",
    "ClaimTypeMemberDescriptionsStale",
    "claim_type_input_defaults",
    "claim_type_input_template",
    "default_evidence_policy",
    "refuse_added_per_file_rules",
    "refuse_added_family_rules",
    "refuse_retired_sources",
    "ClaimTypeSourcesRetired",
    "SOURCES_RETIRED_REPAIR",
    "ClaimTypeFamilyRuleNotAuthored",
    "WORKSPACE_FILE_FAMILY",
    "ClaimTypePerFileEvidenceRule",
    "default_resolution_policy",
    "identity_evidence_policy",
    "lower_claim_type_input",
]
