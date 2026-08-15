"""Pure shared vocabulary for runtime actor identities."""

from typing import Final, Literal

LOCAL_OPERATOR_ACTOR_ID: Final = "operator"
LOCAL_OPERATOR_ACTOR_TYPE: Final[Literal["human_user"]] = "human_user"
LOCAL_OPERATOR_KIND: Final = "human"
LOCAL_OPERATOR_ORG_ID: Final = "local"
LOCAL_OPERATOR_STATUS: Final = "active"

__all__ = [
    "LOCAL_OPERATOR_ACTOR_ID",
    "LOCAL_OPERATOR_ACTOR_TYPE",
    "LOCAL_OPERATOR_KIND",
    "LOCAL_OPERATOR_ORG_ID",
    "LOCAL_OPERATOR_STATUS",
]
