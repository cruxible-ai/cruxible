"""Service modules for the active Playbill surface.

The package deliberately has no eager re-export catalog. Importing a Playbill
service must not initialize retired graph, group, workflow-apply, or
Procedure-store donors as a side effect. Active callers import the owning
``service.playbill_*`` module directly.
"""
