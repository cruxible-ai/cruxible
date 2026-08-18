"""Claim-native governed query surface.

PC-F slice 1 freezes the declarative grammar and the
``playbill-query-definition-v1`` artifact. Slice 2 adds direct evaluation of an
accepted definition against accepted Subject/Claim facts under one explicit
coordinate and evaluation time. Materialized Subject views and the query
backend land in the following slice; nothing here reads a wall clock or caches.
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
