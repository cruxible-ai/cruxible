"""Residual provider contract types held for the bundled provider readers.

PC-F deleted the provider registry and payload donors along with the workflow
executor that drove them. What survives is the contract surface the
un-transplanted readers in :mod:`cruxible_core.providers` are written against
(:mod:`cruxible_core.provider.types`) and the trace payload retention
primitive it depends on (:mod:`cruxible_core.provider.trace_payloads`). Both
leave with those readers in PC-G.

Nothing is re-exported here: the old lazy ``resolve_provider`` accessor existed
only to break an import cycle through the deleted runtime instance, and every
remaining consumer imports the owning module directly.
"""
