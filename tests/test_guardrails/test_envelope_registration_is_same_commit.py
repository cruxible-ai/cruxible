"""The virtual root's envelope is total, and the v1 verifier is frozen.

Two independent hazards, one file:

* a definition field that is neither in the base envelope nor registered is
  OUTSIDE the digest, so the definition can change without its identity
  changing. Registering it LATER is worse than never: the same definition then
  has two digests, one before the registration and one after;
* ``_compute_definition_digest_v1`` is archival infrastructure. Receipts outlive
  procedures, so a historical receipt's ``definition_digest`` must keep
  resolving after the last v1 procedure is gone. Its body is pinned by source
  hash, which is what makes "refactored it slightly" a failing test rather than
  a silent break of every stored commitment.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
from pathlib import Path

from cruxible_core.procedure import digest as digest_module
from cruxible_core.procedure.digest import (
    BASE_ENVELOPE_FIELDS,
    DIGEST_FUNCTIONS,
    registered_envelope_fields,
)
from cruxible_core.procedure.types import ProcedureDefinition

ENVELOPE_EXEMPT_FIELDS = frozenset({"steps"})
"""The only field deliberately outside the root's own content: the node digests
already commit it through the root's successor."""

FROZEN_V1_DIGEST_SOURCE_SHA256 = "be5855f11e9ce6fa846afc140995e2485b3e4629e10dfc1dd36344632fec6048"


def test_every_definition_field_is_committed_or_deliberately_exempt() -> None:
    committed = set(BASE_ENVELOPE_FIELDS) | set(registered_envelope_fields())
    uncommitted = sorted(set(ProcedureDefinition.model_fields) - committed - ENVELOPE_EXEMPT_FIELDS)
    assert uncommitted == [], (
        f"{uncommitted} are definition fields outside the v2 digest envelope. "
        "Register each in the SAME commit that declares it -- registering later "
        "gives one definition two digests -- or add it to the exemption list "
        "with the reason its content is already committed elsewhere."
    )


def test_the_exemption_list_stays_at_one_entry() -> None:
    assert ENVELOPE_EXEMPT_FIELDS == frozenset({"steps"})


def test_no_registered_envelope_field_shadows_a_base_field() -> None:
    overlap = sorted(set(registered_envelope_fields()) & set(BASE_ENVELOPE_FIELDS))
    assert overlap == [], f"{overlap} are registered and already in the base envelope"


def test_the_frozen_v1_digest_function_body_has_not_changed() -> None:
    source = inspect.getsource(DIGEST_FUNCTIONS[1])
    tree = ast.parse(source.lstrip())
    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)
    # Docstrings are prose about the freeze, not part of it.
    body = [node for node in function.body if not _is_docstring(node)]
    # Python 3.13 stopped rendering empty AST fields by default. Preserve the
    # 3.11/3.12 representation used for the frozen hash so this guard checks
    # the function body rather than the interpreter running the test.
    dump_options = (
        {"show_empty": True} if "show_empty" in inspect.signature(ast.dump).parameters else {}
    )
    normalized = "\n".join(ast.dump(node, **dump_options) for node in body)
    actual = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    assert actual == FROZEN_V1_DIGEST_SOURCE_SHA256, (
        "the format-v1 digest function changed. Every stored definition_digest "
        "on every shipped instance was computed by the previous body, and five "
        "call sites compare against those stored values. If this change is "
        "genuinely intended it is a release-blocking event requiring a decision "
        "record and a regenerated corpus -- which the corpus itself forbids."
    )


def _is_docstring(node: ast.stmt) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)


def test_the_digest_registry_is_annotated_for_mypy_enforcement() -> None:
    """The ``dict[int, ...]`` annotation is load-bearing, not documentation.

    ``definition_format_version`` returns ``(version, warnings)``. Indexing the
    registry with that tuple raises KeyError for BOTH formats -- at every one of
    the five recompute sites -- and the annotation is what makes mypy catch it
    instead of a shipped instance.
    """
    source = Path(digest_module.__file__).read_text()
    assert "DIGEST_FUNCTIONS: dict[int, Callable[[ProcedureDefinition], str]]" in source
