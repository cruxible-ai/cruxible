"""Opt-in Playbill ledger substrate.

PB-A intentionally exposes only internal/library initialization, reopening, and
inspection. Public CLI/HTTP/MCP surfaces arrive with the Family-1 vertical
slice after proposal and activation semantics exist.
"""

from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.keys import generate_client_principal_key
from cruxible_core.playbill.types import PlaybillTrustRoot, PrincipalRecord

__all__ = [
    "PlaybillInstance",
    "PlaybillTrustRoot",
    "PrincipalRecord",
    "generate_client_principal_key",
]
