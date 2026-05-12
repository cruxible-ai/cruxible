"""Shared workflow execution action and result type aliases."""

from __future__ import annotations

from typing import Literal

WorkflowExecutionAction = Literal["run", "preview", "apply"]
"""Actions accepted by the low-level workflow executor."""

WorkflowResultMode = Literal["run", "preview", "apply", "proposal"]
"""Mode recorded in workflow execution results and receipts."""
