"""Frozen workflow compiler surface retained as a PC-E2 behavior oracle.

Execution and governed-apply exports were removed in PC-D. Importing this
package therefore cannot initialize the retired proposal/apply machinery.
"""

from cruxible_core.workflow.compiler import (
    LOCK_FILE_NAME,
    build_lock,
    compile_plan_definition,
    compile_workflow,
    compute_lock_config_digest,
    compute_lock_digest,
    get_lock_path,
    load_lock,
    resolve_lock_path,
    write_lock,
)
from cruxible_core.workflow.types import (
    CompiledPlan,
    CompiledPlanStep,
    LockedArtifact,
    LockedProvider,
    WorkflowExecutionResult,
    WorkflowLock,
)

__all__ = [
    "LOCK_FILE_NAME",
    "CompiledPlan",
    "CompiledPlanStep",
    "LockedArtifact",
    "LockedProvider",
    "WorkflowExecutionResult",
    "WorkflowLock",
    "build_lock",
    "compile_plan_definition",
    "compile_workflow",
    "compute_lock_config_digest",
    "compute_lock_digest",
    "get_lock_path",
    "load_lock",
    "resolve_lock_path",
    "write_lock",
]
