"""Claim-native governed query surface.

PC-F slice 1 freezes the declarative grammar and the
``playbill-query-definition-v1`` artifact. Slice 2 adds direct evaluation of an
accepted definition against accepted Subject/Claim facts under one explicit
coordinate and evaluation time. Slice 3 adds the materialized Subject view, the
backend contract every evaluation reads through, and the local NetworkX
backend. Backends are caches: they are built only from accepted facts, they can
be deleted and rebuilt without touching the ledger, and nothing here reads a
wall clock or picks a winner among competing accepted Claims.
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
