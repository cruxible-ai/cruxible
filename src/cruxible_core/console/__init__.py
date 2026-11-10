"""`cruxible console`: serve the pre-built read-only console from the installed package.

The listener (``listener.py``) binds loopback only, serves the console's built
files (``bundle.py``) and forwards the console's read routes (``routes.py``) to
the daemon the CLI context resolves. Nothing else reaches the daemon.
"""
