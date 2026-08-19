"""Residual workflow lock/plan types held for the Procedure pin payload.

PC-F deleted the workflow compiler, contract, transform, artifact, and ref
modules together with the rest of the query-oracle spine. One module survives,
:mod:`cruxible_core.workflow.types`, because ``procedure/pins.py`` still
describes what a pin records in terms of ``WorkflowLock``, ``LockedProvider``,
and ``LockedArtifact``. It leaves with the Procedure donor in PC-H.

Nothing is re-exported here: an eager catalog would make importing the package
pull in the whole Procedure/receipt type graph, and both remaining consumers
import the owning module directly.
"""
