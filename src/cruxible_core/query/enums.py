"""Lightweight query enum contracts."""

from __future__ import annotations

from typing import Literal

QueryResultShape = Literal["entity", "path", "relationship"]
QueryDedupe = Literal["entity", "path", "none"]

__all__ = ["QueryDedupe", "QueryResultShape"]
