"""Lightweight query enum contracts."""

from __future__ import annotations

from typing import Literal

QueryResultShape = Literal["entity", "path", "relationship"]
QueryDedupe = Literal["entity", "path", "none"]
QueryRelationshipState = Literal["live", "accepted", "pending"]

__all__ = ["QueryDedupe", "QueryRelationshipState", "QueryResultShape"]
