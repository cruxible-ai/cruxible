"""Error hierarchy for Cruxible Core.

All exceptions inherit from CoreError. Two intermediate base classes
separate config-level errors (schema definitions) from graph-level
errors (runtime data), making it easy to catch by category.

    CoreError
    ├── SchemaError (config definition problems)
    │   ├── ConfigError
    │   ├── EntityTypeNotFoundError
    │   ├── RelationshipNotFoundError
    │   └── QueryNotFoundError
    ├── GraphError (runtime data problems)
    │   ├── EntityNotFoundError
    │   ├── DataValidationError
    │   ├── RelationshipAmbiguityError
    │   └── ConstraintViolationError
    ├── ExecutionError (operation failures)
    │   ├── IngestionError
    │   ├── MutationError
    │   ├── QueryExecutionError
    │   ├── CustomerCodeExecutionUnsupportedError
    │   └── TransportError
    ├── OwnershipError (overlay type-level ownership)
    ├── ReceiptNotFoundError (receipt store lookup)
    ├── TraceNotFoundError (trace store lookup)
    ├── OutcomeNotFoundError (feedback store lookup)
    ├── InstanceNotFoundError (instance registry lookup)
    ├── GroupNotFoundError (group store lookup)
    ├── ProcedureNotFoundError (procedure store lookup)
    ├── InstallNotFoundError (install ledger lookup)
    ├── InstallPhaseTransitionError (illegal install phase advance)
    ├── InstallPhaseRequirementError (install op attempted from the wrong phase)
    ├── InstallOwnershipCollisionError (object name already owned by an install)
    ├── BindingNotFoundError (compute-slot binding ledger lookup)
    ├── SlotAlreadyBoundError (slot already carries an active binding)
    ├── SlotBindingRefusedError (provider does not satisfy the slot interface)
    │   ├── BindingContractMismatchError (contracts differ; carries near matches)
    │   ├── BindingBillingModeRefusedError (billing mode outside the allowed set)
    │   ├── BindingConsentRequiredError (third-party bind without recorded consent)
    │   ├── BindingConsentNotAttributableError (consent asserted with no actor)
    │   └── BindingSlotInterfaceMismatchError (rebind restated the pinned interface)
    ├── RuntimeCredentialNotFoundError (server credential store lookup)
    ├── AuthenticationError (HTTP/API credential failure)
    ├── InstanceScopeError (HTTP/API credential scope mismatch)
    ├── PermissionDeniedError (MCP permission mode)
    ├── DirectWriteRefusedError (governed proposal_only direct-write refusal)
    │   ├── GroupApprovedContentWriteRefusedError (content change to an approved edge)
    │   └── GovernedSourceSpoofRefusedError (direct write naming a governed verb)
    ├── TerminalLifecycleWriteRefusedError (terminal lifecycle via a free write)
    ├── PendingEdgeWriteRefusedError (non-pending write onto a pending proposal)
    └── ConcurrentStateDriftError (live state moved under a `current` diff coordinate)
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from cruxible_client._error_base import (
    ConcurrentStateDriftError as _ConcurrentStateDriftError,
)
from cruxible_client._error_base import (
    CoreError as CoreError,
)
from cruxible_client._error_base import (
    InvalidContinuationError as _InvalidContinuationError,
)
from cruxible_client._error_base import (
    StaleContinuationError as _StaleContinuationError,
)

# The base is an identity alias, not a parallel subclass. Every reconstructed
# client exception and every locally raised core exception therefore shares the
# exact same catch surface in-process.
ConcurrentStateDriftError = _ConcurrentStateDriftError
InvalidContinuationError = _InvalidContinuationError
StaleContinuationError = _StaleContinuationError


# ---------------------------------------------------------------------------
# Schema errors — config definition is wrong or missing
# ---------------------------------------------------------------------------


class SchemaError(CoreError):
    """Base for errors in the config schema definition."""

    pass


_MAX_DISPLAY_ERRORS = 10


def _format_capped_errors(errors: list[str]) -> str:
    shown = errors[:_MAX_DISPLAY_ERRORS]
    detail = "; ".join(shown)
    if len(errors) > _MAX_DISPLAY_ERRORS:
        detail += f" ... and {len(errors) - _MAX_DISPLAY_ERRORS} more error(s)"
    return detail


class ConfigError(SchemaError):
    """Invalid configuration YAML.

    Raised when config fails schema validation or cross-reference checks.
    """

    def __init__(
        self,
        message: str,
        errors: list[str] | None = None,
        *,
        mutation_receipt_id: str | None = None,
    ):
        self.summary = message
        self.errors = errors or []
        super().__init__(message, mutation_receipt_id=mutation_receipt_id)

    def __str__(self) -> str:
        if not self.errors:
            return self.summary + self._receipt_suffix()
        detail = _format_capped_errors(self.errors)
        return f"{self.summary}: {detail}" + self._receipt_suffix()


class ReservedSubjectError(ConfigError):
    """The public contract opener named a live core-owned subject kind."""

    error_code = "reserved_subject"


class RetiredReservedKindError(ConfigError):
    """A contract opener named a retired core-owned subject kind."""

    error_code = "retired_reserved_kind"


class UnknownReservedSubjectError(ConfigError):
    """A contract opener named an unregistered reserved subject kind."""

    error_code = "unknown_reserved_subject"


class MalformedReservedSubjectError(ConfigError):
    """A contract opener used malformed reserved-subject syntax."""

    error_code = "malformed_reserved_subject"


class ContractGradeRefusedError(ConfigError):
    """A procedure reading did not prove its declared contract-grade coordinates."""

    error_code = "contract_grade_refused"


class EntityTypeNotFoundError(SchemaError):
    """Entity type not defined in config schema."""

    def __init__(self, entity_type: str, *, known_entity_types: list[str] | None = None):
        self.entity_type = entity_type
        self.known_entity_types = sorted(known_entity_types or [])
        message = f"Entity type '{entity_type}' not found in schema"
        if self.known_entity_types:
            message += f". Known entity types: {', '.join(self.known_entity_types)}"
        super().__init__(message)


class RelationshipNotFoundError(SchemaError):
    """Relationship type not defined in config schema."""

    def __init__(self, relationship_name: str):
        self.relationship_name = relationship_name
        super().__init__(f"Relationship '{relationship_name}' not found in schema")


class QueryNotFoundError(SchemaError):
    """Named query not defined in config schema."""

    def __init__(self, query_name: str):
        self.query_name = query_name
        super().__init__(f"Named query '{query_name}' not found in schema")


# ---------------------------------------------------------------------------
# Graph errors — runtime data is wrong or missing
# ---------------------------------------------------------------------------


class GraphError(CoreError):
    """Base for errors in graph data at runtime."""

    pass


class EntityNotFoundError(GraphError):
    """Entity with given ID not found in the graph."""

    def __init__(self, entity_type: str, entity_id: str):
        self.entity_type = entity_type
        self.entity_id = entity_id
        super().__init__(f"{entity_type} '{entity_id}' not found in graph")


class DataValidationError(GraphError):
    """Ingested data doesn't match config schema.

    Raised when CSV/JSON data doesn't conform to the entity/relationship
    property definitions in the config (wrong columns, bad types, etc.).
    """

    def __init__(
        self,
        message: str,
        errors: list[str] | None = None,
        *,
        mutation_receipt_id: str | None = None,
    ):
        self.summary = message
        self.errors = errors or []
        super().__init__(message, mutation_receipt_id=mutation_receipt_id)

    def __str__(self) -> str:
        if not self.errors:
            return self.summary + self._receipt_suffix()
        detail = _format_capped_errors(self.errors)
        return f"{self.summary}: {detail}" + self._receipt_suffix()


class RelationshipAmbiguityError(GraphError):
    """A relationship target is ambiguous and needs a stable edge key."""

    def __init__(
        self,
        from_type: str,
        from_id: str,
        to_type: str,
        to_id: str,
        relationship_type: str,
    ):
        self.from_type = from_type
        self.from_id = from_id
        self.to_type = to_type
        self.to_id = to_id
        self.relationship_type = relationship_type
        super().__init__(
            "Ambiguous relationship target for "
            f"{from_type}:{from_id}:{relationship_type}:{to_type}:{to_id}; "
            "specify edge_key to target a single edge"
        )


class ConstraintViolationError(GraphError):
    """Constraint rule was violated."""

    def __init__(
        self,
        message: str,
        violations: list[str] | None = None,
        *,
        mutation_receipt_id: str | None = None,
    ):
        self.summary = message
        self.violations = violations or []
        super().__init__(message, mutation_receipt_id=mutation_receipt_id)

    def __str__(self) -> str:
        if not self.violations:
            return self.summary + self._receipt_suffix()
        detail = _format_capped_errors(self.violations)
        return f"{self.summary}: {detail}" + self._receipt_suffix()


# ---------------------------------------------------------------------------
# Execution errors — operation failures
# ---------------------------------------------------------------------------


class ExecutionError(CoreError):
    """Base for errors during operation execution."""

    pass


class IngestionError(ExecutionError):
    """Error during data ingestion.

    Raised when CSV parsing, column mapping, or data normalization fails.
    """

    pass


class MutationError(ExecutionError):
    """Unexpected failure during a graph mutation.

    Raised when durable writes (save_graph, store writes) fail for reasons
    other than data validation (OSError, sqlite3 errors, etc.).
    """

    pass


class QueryExecutionError(ExecutionError):
    """Error during query execution.

    Raised when query setup fails (missing parameters, no primary key,
    entry entity type not in config, etc.). The query exists in config
    but cannot be executed with the given inputs.
    """

    def __init__(self, message: str):
        super().__init__(message)


class CustomerCodeExecutionUnsupportedError(ExecutionError):
    """Customer code execution is unavailable in the current hosted runtime."""

    error_code = "customer_code_execution_unsupported"

    def __init__(self) -> None:
        super().__init__("Customer code execution is not supported in this hosted runtime profile.")


class TransportError(ExecutionError):
    """Error during state release transport operations."""

    pass


class ProcedureBudgetExceededError(QueryExecutionError):
    """A procedure exhausted one of its declared execution budgets."""

    def __init__(self, message: str):
        self.budget_exceeded = True
        super().__init__(message)


class ProcedureRepeatExhaustedError(QueryExecutionError):
    """A bounded procedure repeat ended without satisfying its condition."""

    def __init__(self, step_id: str, max_attempts: int):
        self.repeat_exhausted = True
        self.step_id = step_id
        self.max_attempts = max_attempts
        super().__init__(
            f"Procedure repeat step '{step_id}' exhausted {max_attempts} attempt(s) "
            "without satisfying its until condition"
        )


class ProcedureWithdrawalRefusedError(CoreError):
    """Withdraw refused: the actor is neither the proposal's author nor a reviewer.

    Withdrawal is the AUTHOR's retraction of their own pending proposal, so it
    sits at the proposing tier rather than the review tier: an agent that
    changed its mind about a definition it just proposed does not need a
    reviewer to unblock it. Anyone else reaching for the same verb is
    adjudicating someone else's proposal, which is the review act, so it
    carries the reviewer tier that ``accept``/``reject`` require.

    Distinct from :class:`PermissionDeniedError` on purpose: the refusal is not
    "this verb needs a higher tier" (the author is admitted at their own), it is
    "this verb needs the author OR the reviewer tier, and you are neither". The
    message names both halves of that rule and the identity it compared.
    """

    error_code = "procedure_withdrawal_refused"

    def __init__(
        self,
        procedure_id: str,
        *,
        current_mode: str,
        required_mode: str,
        message: str | None = None,
    ):
        self.procedure_id = procedure_id
        self.current_mode = current_mode
        self.required_mode = required_mode
        super().__init__(
            message
            or (
                f"procedure '{procedure_id}' may be withdrawn only by its proposing author "
                f"at their own tier, or by a reviewer holding {required_mode}; "
                f"the current actor is neither (current mode {current_mode})"
            )
        )


class OwnershipError(CoreError):
    """Write rejected because the target type is upstream-owned in a overlay instance."""

    def __init__(self, message: str, *, blocked_types: list[str] | None = None):
        self.blocked_types = blocked_types or []
        super().__init__(message)


# ---------------------------------------------------------------------------
# Store errors — persistence lookups
# ---------------------------------------------------------------------------


class ReceiptNotFoundError(CoreError):
    """Receipt ID not found in store."""

    def __init__(self, receipt_id: str):
        self.receipt_id = receipt_id
        super().__init__(f"Receipt '{receipt_id}' not found")


class TraceNotFoundError(CoreError):
    """Execution trace ID not found in store."""

    def __init__(self, trace_id: str):
        self.trace_id = trace_id
        super().__init__(f"Trace '{trace_id}' not found")


class OutcomeNotFoundError(CoreError):
    """Outcome for a receipt was not found in the feedback store."""

    def __init__(self, receipt_id: str):
        self.receipt_id = receipt_id
        super().__init__(f"No outcome found for receipt '{receipt_id}'")


class InstanceNotFoundError(CoreError):
    """Cruxible instance not found."""

    def __init__(self, instance_id: str):
        self.instance_id = instance_id
        super().__init__(f"Instance '{instance_id}' not found")


class GroupNotFoundError(CoreError):
    """Group ID not found in store."""

    def __init__(self, group_id: str):
        self.group_id = group_id
        super().__init__(f"Group '{group_id}' not found")


class ProcedureNotFoundError(CoreError):
    """Procedure ID not found in store."""

    def __init__(self, procedure_id: str):
        self.procedure_id = procedure_id
        super().__init__(f"Procedure '{procedure_id}' not found")


class InstallNotFoundError(CoreError):
    """Install ID not found in the install ledger."""

    def __init__(self, install_id: str):
        self.install_id = install_id
        super().__init__(f"Install '{install_id}' not found")


class InstallPhaseTransitionError(CoreError):
    """Refused install phase transition.

    The message names the phase the install is ACTUALLY in, not the phase the
    caller assumed. An installer resuming after a crash has no other way to
    learn where it got to, and a bare "invalid transition" would send it
    guessing — so the actual phase and its legal successors both ride on the
    exception and in the message.
    """

    error_code = "install_phase_transition_refused"

    def __init__(
        self,
        install_id: str,
        actual_phase: str,
        requested_phase: str,
        legal_phases: Sequence[str],
    ):
        self.install_id = install_id
        self.actual_phase = actual_phase
        self.requested_phase = requested_phase
        self.legal_phases = list(legal_phases)
        allowed = ", ".join(self.legal_phases) if self.legal_phases else "none (terminal phase)"
        super().__init__(
            f"Install '{install_id}' is in phase '{actual_phase}' and cannot move to "
            f"'{requested_phase}'; legal next phases: {allowed}"
        )


class InstallPhaseRequirementError(CoreError):
    """An install operation was attempted from a phase that does not permit it.

    Distinct from :class:`InstallPhaseTransitionError`, which is about MOVING
    between phases. This one is about an operation (claiming ownership, say)
    that is only legal while the install sits in a particular phase.
    """

    error_code = "install_phase_requirement_unmet"

    def __init__(
        self,
        install_id: str,
        operation: str,
        actual_phase: str,
        required_phases: Sequence[str],
    ):
        self.install_id = install_id
        self.operation = operation
        self.actual_phase = actual_phase
        self.required_phases = list(required_phases)
        required = " or ".join(f"'{phase}'" for phase in self.required_phases)
        super().__init__(
            f"Install '{install_id}' is in phase '{actual_phase}'; {operation} requires "
            f"phase {required}"
        )


class InstallOwnershipCollisionError(CoreError):
    """An installable object name is already claimed by another live install."""

    error_code = "install_ownership_collision"

    def __init__(
        self,
        object_kind: str,
        object_name: str,
        owning_install_id: str,
        owning_install_phase: str,
    ):
        self.object_kind = object_kind
        self.object_name = object_name
        self.owning_install_id = owning_install_id
        self.owning_install_phase = owning_install_phase
        super().__init__(
            f"{object_kind} '{object_name}' is already owned by install "
            f"'{owning_install_id}' (phase '{owning_install_phase}')"
        )


class BindingNotFoundError(CoreError):
    """No binding ledger row for the requested slot, install, or binding ID.

    Distinct from an unbound slot being an ordinary empty read: resolution asks
    for a binding the caller needs in order to proceed, so "this install never
    bound this slot" is the answer to a question that had to have one.
    """

    error_code = "binding_not_found"

    def __init__(
        self,
        *,
        install_id: str | None = None,
        slot_name: str | None = None,
        binding_id: str | None = None,
    ) -> None:
        self.install_id = install_id
        self.slot_name = slot_name
        self.binding_id = binding_id
        if binding_id is not None:
            message = f"Binding '{binding_id}' not found"
        else:
            message = (
                f"no active binding for slot '{slot_name}' on install '{install_id}'; "
                "bind the slot to a provider before running a procedure that names it"
            )
        super().__init__(message)


class SlotAlreadyBoundError(CoreError):
    """The slot already carries an active binding on this install.

    Binding twice is not a rebind: a rebind is an explicit, receipted revision
    of the SAME ledger row, and routing it through create would leave two active
    rows racing to be the one run-start resolves. The database refuses the
    second row regardless (partial unique index); this error is what that
    refusal looks like when the service sees it first.
    """

    error_code = "slot_already_bound"

    def __init__(
        self,
        *,
        install_id: str,
        slot_name: str,
        binding_id: str | None = None,
        provider_name: str | None = None,
    ) -> None:
        self.install_id = install_id
        self.slot_name = slot_name
        self.binding_id = binding_id
        self.provider_name = provider_name
        bound_to = f" to provider '{provider_name}'" if provider_name else ""
        super().__init__(
            f"slot '{slot_name}' on install '{install_id}' is already bound{bound_to}; "
            "use rebind to move it to another provider, or retire the binding first"
        )


class SlotBindingRefusedError(CoreError):
    """Base for a refused bind/rebind: the provider does not satisfy the slot."""

    error_code = "slot_binding_refused"


class BindingContractMismatchError(SlotBindingRefusedError):
    """The provider's declared contracts are not the slot interface.

    Equality, not compatibility: the slot interface names a contract in and a
    contract out, and a provider satisfies it only by declaring exactly those.
    A provider that "nearly" fits is reported as a NEAR MATCH with every reason
    it failed, because the operator's next action is choosing a different
    provider and they need the whole list to choose from once.
    """

    error_code = "binding_contract_mismatch"

    def __init__(
        self,
        *,
        install_id: str,
        slot_name: str,
        report_text: str,
        near_matches: list[dict[str, Any]] | None = None,
    ) -> None:
        self.install_id = install_id
        self.slot_name = slot_name
        self.report_text = report_text
        self.near_matches = near_matches or []
        super().__init__(report_text)


class BindingBillingModeRefusedError(SlotBindingRefusedError):
    """The provider's billing mode is outside the slot's allowed set."""

    error_code = "binding_billing_mode_refused"

    def __init__(
        self,
        *,
        install_id: str,
        slot_name: str,
        provider_name: str,
        billing_mode: str,
        allowed_billing_modes: list[str],
    ) -> None:
        self.install_id = install_id
        self.slot_name = slot_name
        self.provider_name = provider_name
        self.billing_mode = billing_mode
        self.allowed_billing_modes = allowed_billing_modes
        super().__init__(
            f"provider '{provider_name}' declares billing_mode '{billing_mode}', which "
            f"slot '{slot_name}' does not allow; allowed values: "
            f"{', '.join(allowed_billing_modes)}"
        )


class BindingConsentRequiredError(SlotBindingRefusedError):
    """A third-party provider was bound without recorded operator consent.

    The consent is recorded ON the binding with the actor who gave it and when,
    because that is the record an audit asks for later: not "the config said
    third parties were fine" but "this operator accepted this provider for this
    slot at this time".
    """

    error_code = "binding_consent_required"

    def __init__(
        self,
        *,
        install_id: str,
        slot_name: str,
        provider_name: str,
    ) -> None:
        self.install_id = install_id
        self.slot_name = slot_name
        self.provider_name = provider_name
        super().__init__(
            f"slot '{slot_name}' requires recorded third-party consent to bind "
            f"provider '{provider_name}'; pass third_party_consent=True with an actor "
            "context, so the consenting actor and timestamp are recorded on the binding"
        )


class BindingConsentNotAttributableError(SlotBindingRefusedError):
    """Third-party consent was asserted with nobody to attribute it to.

    An unattributed consent stamp is worse than no stamp: it reads as an
    approval on the binding while naming no approver, so the audit question the
    stamp exists to answer ("who accepted this vendor, and when") comes back
    empty from a record that claims to hold it. Consent is refused rather than
    recorded anonymously.
    """

    error_code = "binding_consent_not_attributable"

    def __init__(
        self,
        *,
        install_id: str,
        slot_name: str,
        provider_name: str,
    ) -> None:
        self.install_id = install_id
        self.slot_name = slot_name
        self.provider_name = provider_name
        super().__init__(
            f"third-party consent for provider '{provider_name}' on slot '{slot_name}' "
            "was asserted without an actor context; consent is recorded with the actor "
            "who gave it, so an unattributable consent is refused rather than stamped"
        )


class BindingSlotInterfaceMismatchError(SlotBindingRefusedError):
    """A rebind described a different slot interface than the binding pinned.

    A rebind is a DEPLOYMENT decision: it moves a slot to another provider and
    changes nothing about what the slot is. The interface a binding was created
    against is stored on the ledger row and is what every later rebind is
    checked against — a request that restates it differently is refused, and
    told what the ledger actually holds, rather than being allowed to redefine
    the contract it is being checked against.
    """

    error_code = "binding_slot_interface_mismatch"

    def __init__(
        self,
        *,
        install_id: str,
        slot_name: str,
        binding_id: str,
        stored_interface: dict[str, Any],
        supplied_interface: dict[str, Any],
        differences: list[str],
    ) -> None:
        self.install_id = install_id
        self.slot_name = slot_name
        self.binding_id = binding_id
        self.stored_interface = stored_interface
        self.supplied_interface = supplied_interface
        self.differences = differences
        super().__init__(
            f"rebind of slot '{slot_name}' on install '{install_id}' supplied a different "
            f"slot interface than binding '{binding_id}' pinned at bind time: "
            + "; ".join(differences)
            + ". A rebind moves the provider, never the interface; to change the "
            "interface, retire this binding and bind the new one."
        )


class CitationHandleResolutionError(CoreError):
    """A source-evidence citation handle could not be resolved safely."""

    error_code = "citation_handle_resolution_failed"

    def __init__(
        self,
        handle: str,
        failure_kind: str,
        *,
        detail: str,
    ) -> None:
        self.handle = handle
        self.failure_kind = failure_kind
        self.detail = detail
        super().__init__(
            f"Citation handle resolution failed ({failure_kind}) for '{handle}': {detail}"
        )


class RuntimeCredentialNotFoundError(CoreError):
    """Runtime credential ID not found in the server credential store."""

    def __init__(self, credential_id: str):
        self.credential_id = credential_id
        super().__init__(f"Runtime credential '{credential_id}' not found")


class AuthenticationError(CoreError):
    """HTTP/API request is unauthenticated or uses an invalid credential."""

    pass


class InstanceScopeError(CoreError):
    """Runtime credential scope does not match the requested instance."""

    def __init__(self, instance_id: str, credential_scope: str):
        self.instance_id = instance_id
        self.credential_scope = credential_scope
        super().__init__(
            f"Credential scoped to instance '{credential_scope}' cannot access "
            f"instance '{instance_id}'"
        )


# ---------------------------------------------------------------------------
# Permission errors
# ---------------------------------------------------------------------------


class PermissionDeniedError(CoreError):
    """Operation denied due to insufficient effective permission mode."""

    def __init__(
        self,
        tool_name: str,
        current_mode: str,
        required_mode: str,
        *,
        ceiling_mode: str | None = None,
    ):
        self.tool_name = tool_name
        self.current_mode = current_mode
        self.required_mode = required_mode
        self.ceiling_mode = ceiling_mode
        if ceiling_mode is not None:
            super().__init__(
                f"Operation '{tool_name}' requires {required_mode} mode, but the daemon "
                f"capability ceiling is {ceiling_mode} mode "
                f"(effective request mode: {current_mode})"
            )
            return
        super().__init__(
            f"Tool '{tool_name}' requires {required_mode} mode, "
            f"but server is running in {current_mode} mode"
        )


class DirectWriteRefusedError(CoreError):
    """Direct graph write refused because the target policy disallows the source.

    A HARD governance constraint, independent of permission tier (even
    ``CRUXIBLE_MODE=admin`` is refused). State for a ``proposal_only`` type may
    only enter through the governed proposal/workflow path; relationship writes
    may also be staged with ``pending=true``. State for a ``mint_only`` type may
    only enter through runtime credential minting.

    ``kind="feedback"`` is the feedback-channel arm of the same
    ``CRUXIBLE_REFUSE_DIRECT_WRITES`` kill-switch: an ``approve``/``correct``
    feedback action transitions an edge INTO accepted state, which is the same
    governance event as a direct live write, so the daemon-wide kill-switch
    refuses it too. ``source`` carries the feedback action.
    """

    error_code = "direct_write_refused"

    def __init__(
        self,
        kind: str,
        type_name: str,
        source: str,
        *,
        policy: str = "proposal_only",
    ):
        self.kind = kind
        self.type_name = type_name
        self.source = source
        self.policy = policy
        if kind == "feedback":
            super().__init__(
                f"Feedback action '{source}' on relationship '{type_name}' is refused: "
                "it transitions the edge into accepted state while "
                "CRUXIBLE_REFUSE_DIRECT_WRITES is set daemon-wide. Clear the "
                "kill-switch to adjudicate, or leave the edge staged for review."
            )
            return
        if policy == "mint_only":
            super().__init__(
                f"Direct write to {kind} '{type_name}' is refused "
                f"(write_policy=mint_only). This auth-managed type is writable "
                f"only via credential mint (`cruxible credential mint`)."
            )
            return
        if kind == "relationship":
            forward = (
                "Use 'group propose' to stage a governed proposal, or pass "
                "pending=true to stage the edge for review."
            )
        else:
            forward = (
                "Add it through a governed canonical workflow (apply_entities) "
                "instead of a direct write."
            )
        super().__init__(
            f"Direct write to {kind} '{type_name}' is refused "
            f"(write_policy=proposal_only). {forward}"
        )


class TerminalLifecycleWriteRefusedError(CoreError):
    """A terminal lifecycle status was refused on a free-form add/update.

    Retracting, superseding, or retiring is a governed judgement about a claim's
    standing, not a property edit. Reachable from a plain add/update it was a
    one-call way to make live state vanish from every live-gated read with no
    reviewer, no required reason, and nothing recording who decided it.

    The dedicated receipted lifecycle verbs own these transitions. Non-terminal
    statuses (relationship ``active``/``inactive``, entity ``live``) stay freely
    writable because they are reversible participation flips, not settled acts.

    The teaching message is KIND-AWARE: a refused entity write names the entity
    verbs and a refused relationship write names the relationship verbs. Naming
    all four sent the caller to read half a message that could not apply to the
    write they made.
    """

    error_code = "terminal_lifecycle_write_refused"

    _VERBS: dict[str, str] = {
        "entity": "'cruxible entity supersede' or 'cruxible entity retire'",
        "relationship": ("'cruxible relationship supersede' or 'cruxible relationship retract'"),
    }

    def __init__(self, kind: str, status: str, writable: str):
        self.kind = kind
        self.status = status
        self.writable = writable
        verbs = self._VERBS.get(
            kind,
            "the dedicated receipted lifecycle verbs",
        )
        super().__init__(
            f"Refusing to write terminal {kind} lifecycle status '{status}' through a "
            f"plain add/update: terminal lifecycle transitions require {verbs}, which "
            "carry a required reason, actor attribution, and a mutation receipt. "
            f"Writable here: {writable}."
        )


class GroupApprovedContentWriteRefusedError(DirectWriteRefusedError):
    """A direct write would change the CONTENT of a group-approved edge.

    Acceptance binds content: when a group approval accepted an edge it accepted
    that edge's properties, not merely its existence. For a ``proposal_only``
    relationship type the only legitimate way to change those properties is to
    re-propose them, so a content-changing direct write is refused here.

    This is a strictly NARROWER refusal than the plain
    :class:`DirectWriteRefusedError` at ``graph/operations.py``, and it exists
    because that one has a hole: it exempts the governed sources
    (``workflow_apply`` / ``group_resolve``), and ``source`` is caller-supplied
    at the public direct-write API (``add_relationships_with_provenance``). An
    actor could therefore name a governed source and rewrite a group-approved
    ``proposal_only`` edge with no proposal at all. Subclassing keeps the HTTP
    status (403) and the "refused direct write" taxonomy while carrying the
    group identity the plain refusal cannot name.

    Only raised where the plain chokepoint refusal would NOT fire; it never
    shadows it.
    """

    error_code = "group_approved_content_write_refused"

    def __init__(
        self,
        relationship_type: str,
        from_type: str,
        from_id: str,
        to_type: str,
        to_id: str,
        *,
        group_id: str,
        changed_properties: list[str],
    ):
        self.kind = "relationship"
        self.type_name = relationship_type
        self.source = "direct_write"
        self.policy = "proposal_only"
        self.relationship_type = relationship_type
        self.from_type = from_type
        self.from_id = from_id
        self.to_type = to_type
        self.to_id = to_id
        self.group_id = group_id
        self.changed_properties = list(changed_properties)
        changed = ", ".join(self.changed_properties) or "(none)"
        # Bypass DirectWriteRefusedError.__init__: it composes the generic
        # policy message, and this refusal must TEACH — name the edge, name the
        # approving group, and say what to do instead.
        CoreError.__init__(
            self,
            f"Direct write to relationship '{relationship_type}' "
            f"({from_type}:{from_id} -> {to_type}:{to_id}) is refused: group "
            f"'{group_id}' approved this edge, and approval binds its CONTENT, "
            f"not just its existence. This write changes {changed}, and "
            f"'{relationship_type}' is a proposal_only type — the change must be "
            "re-proposed and re-approved (group propose -> group resolve), not "
            "written directly.",
        )


class GovernedSourceSpoofRefusedError(DirectWriteRefusedError):
    """A public direct-write entry named a GOVERNED write verb as its source.

    ``source`` is caller-supplied at the public direct-write API
    (``add_relationships_with_provenance`` / ``batch_direct_write``), and the
    chokepoint in ``graph/operations.py`` EXEMPTS the governed verbs
    (``workflow_apply`` / ``group_resolve``) from the ``proposal_only`` refusal.
    Naming one of them therefore let a bare direct write create brand-new
    ``proposal_only`` relationships and write ``proposal_only`` entities with no
    proposal, no workflow, and no reviewer anywhere in the act.

    Closed at the SEAM rather than at the chokepoint: the genuine governed paths
    (``service/group_transitions.py`` and ``workflow/apply.py``) call
    ``apply_entity`` / ``apply_relationship`` directly and never route through
    these public entries, so refusing the names here costs them nothing while
    removing the only way to borrow their authority.
    """

    error_code = "governed_source_spoof_refused"

    def __init__(self, source: str, *, entry_point: str):
        self.kind = "relationship"
        self.type_name = source
        self.source = source
        self.policy = "proposal_only"
        self.entry_point = entry_point
        CoreError.__init__(
            self,
            f"Direct write through '{entry_point}' is refused: "
            f"provenance source '{source}' names a GOVERNED write verb. Those "
            "names carry the authority of the proposal and workflow machinery "
            "and are reserved for it. Use 'group propose' -> 'group resolve' to "
            "stage governed state, run the canonical workflow, or pick a "
            "provenance source that honestly describes this direct write.",
        )


class PendingEdgeWriteRefusedError(CoreError):
    """A non-pending write was refused because the target edge is still PENDING.

    ``graph.get_relationship`` is state-blind, so before this refusal a plain
    direct write onto a tuple whose edge was awaiting review resolved to
    ``is_update=True`` and silently replaced the proposal's properties in place
    (wi-pending-edge-clobber). The proposal a reviewer was asked to adjudicate
    is then not the proposal they approve.

    The refusal is a STATE conflict, not a tier or policy problem: the same
    actor may write the tuple freely once the proposal is resolved. It is raised
    at the single relationship chokepoint, so every write path inherits it.
    """

    error_code = "pending_edge_write_refused"

    def __init__(
        self,
        relationship_type: str,
        from_type: str,
        from_id: str,
        to_type: str,
        to_id: str,
    ):
        self.relationship_type = relationship_type
        self.from_type = from_type
        self.from_id = from_id
        self.to_type = to_type
        self.to_id = to_id
        super().__init__(
            f"Write to relationship '{relationship_type}' "
            f"({from_type}:{from_id} -> {to_type}:{to_id}) is refused: the edge is a "
            "PENDING proposal awaiting review, and a non-pending write would replace "
            "the proposal's content while a reviewer is adjudicating it. "
            "If you proposed it: withdraw the proposal, or re-propose the corrected "
            "edge through the pending path (pass pending=true). "
            "If you are reviewing it: resolve it through the review machinery "
            "(feedback approve/reject, or group resolve) and write afterwards."
        )
