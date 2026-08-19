"""Storage package root.

PC-F deleted the SQLite graph repository and the storage protocols it
implemented, so the lazy compatibility export map they backed is gone with
them. :mod:`cruxible_core.storage.playbill_projection` is the only backend
left and is imported directly by its callers.
"""
