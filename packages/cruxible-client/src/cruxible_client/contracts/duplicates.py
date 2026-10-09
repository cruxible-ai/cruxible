"""The post-hoc duplicates audit: what may name one thing twice, for an agent to resolve.

Identity is decided on canonical forms and aliases (identity-design-1009). The
audit looks past them with a broader comparison -- NFKC, casefold, punctuation
stripped, whitespace collapsed and trimmed, plus the alias graph -- and lists,
per Subject kind, what is equal under it but distinct under the declared rules:

- ``subjects``: Subjects of the kind whose ids or aliases meet under the broad
  comparison. Resolve by adding a ``semantic.alias`` to the Subject that stays
  and retiring the other (merge in v1 is alias + retire), or by asserting
  ``semantic.distinct_from`` between them: no group ever holds a pair asserted
  distinct (a key's holders are taken in path order, each joining the first
  group it is asserted distinct from no member of).
- ``values``: live values of one literal predicate of the kind that meet under
  it but are distinct under the predicate's declared canonical form. Resolve by
  a governed ClaimType succession that declares a wider canonical form (its
  dependents re-authored), or by re-authoring the values.

The audit is a read computed on demand, never on the write path; core proposes
no fix. Its work is bounded (``DUPLICATES_AUDIT_MAX_SCANNED``) and stated.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from cruxible_client.contracts.canonical_forms import CanonicalTransform

DUPLICATES_AUDIT_DEFAULT_LIMIT: Final = 25
DUPLICATES_AUDIT_MAX_LIMIT: Final = 200
#: Rows (Subjects, aliases, distinct_from relations and values) one audit reads
#: at most, applied in each SQL read with one overflow sentinel; past it the
#: answer says it is partial (``budget_exhausted``) rather than silently narrowing.
DUPLICATES_AUDIT_MAX_SCANNED: Final = 50_000
#: The broader comparison the audit groups by.
DUPLICATES_AUDIT_BROAD_FORM: Final[tuple[CanonicalTransform, ...]] = (
    "nfkc",
    "casefold",
    "strip_punctuation",
    "collapse_whitespace",
    "trim",
)

DuplicateResolution = Literal["alias_and_retire", "distinct_from", "canonical_form_succession"]


class _StrictDuplicatesModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DuplicatesAuditRequest(_StrictDuplicatesModel):
    """One page of the duplicates audit for one Subject kind."""

    tag: Literal["cruxible-duplicates-audit-request-v1"] = "cruxible-duplicates-audit-request-v1"
    kind: str = Field(min_length=1, max_length=256)
    limit: int = Field(default=DUPLICATES_AUDIT_DEFAULT_LIMIT, ge=1, le=DUPLICATES_AUDIT_MAX_LIMIT)
    cursor: str | None = Field(default=None, max_length=4096)


class DuplicateSubject(_StrictDuplicatesModel):
    """One Subject in a group, with the names (its id and aliases) that met."""

    subject: str
    names: tuple[str, ...]


class DuplicateSubjectGroup(_StrictDuplicatesModel):
    """Subjects of one kind whose ids or aliases meet under the broad comparison."""

    group: Literal["subjects"] = "subjects"
    key: str = Field(description="The broad comparison's spelling the names share.")
    subjects: tuple[DuplicateSubject, ...]
    resolutions: tuple[DuplicateResolution, ...] = ("alias_and_retire", "distinct_from")


class DuplicateValue(_StrictDuplicatesModel):
    """One live Claim holding one of the values that met."""

    claim: str
    subject: str
    value: str


class DuplicateValueGroup(_StrictDuplicatesModel):
    """Values of one predicate equal under the broad comparison, distinct as stored."""

    group: Literal["values"] = "values"
    predicate: str
    key: str = Field(description="The broad comparison's spelling the values share.")
    canonical_form: tuple[CanonicalTransform, ...] = Field(
        description="The predicate's declared canonical form, under which they differ."
    )
    values: tuple[DuplicateValue, ...]
    resolutions: tuple[DuplicateResolution, ...] = ("canonical_form_succession",)


DuplicateGroup = Annotated[
    DuplicateSubjectGroup | DuplicateValueGroup, Field(discriminator="group")
]


class DuplicatesAuditScanned(_StrictDuplicatesModel):
    """The work one audit did: what it read, counted."""

    subjects: int = Field(ge=0)
    aliases: int = Field(ge=0)
    relations: int = Field(ge=0, description="semantic.distinct_from Claims read.")
    values: int = Field(ge=0)


class DuplicatesAuditResult(_StrictDuplicatesModel):
    """One page of possible duplicates of one kind, for an agent to resolve."""

    tag: Literal["cruxible-duplicates-audit-result-v1"] = "cruxible-duplicates-audit-result-v1"
    kind: str
    git_oid: str = Field(description="The accepted coordinate the audit read.")
    groups: tuple[DuplicateGroup, ...] = ()
    scanned: DuplicatesAuditScanned
    budget_exhausted: bool = Field(
        default=False,
        description="The audit stopped reading at its bound; groups past it are not listed.",
    )
    truncated: bool = False
    next_cursor: str | None = None


__all__ = [
    "DUPLICATES_AUDIT_BROAD_FORM",
    "DUPLICATES_AUDIT_DEFAULT_LIMIT",
    "DUPLICATES_AUDIT_MAX_LIMIT",
    "DUPLICATES_AUDIT_MAX_SCANNED",
    "DuplicateGroup",
    "DuplicateResolution",
    "DuplicateSubject",
    "DuplicateSubjectGroup",
    "DuplicateValue",
    "DuplicateValueGroup",
    "DuplicatesAuditRequest",
    "DuplicatesAuditResult",
    "DuplicatesAuditScanned",
]
