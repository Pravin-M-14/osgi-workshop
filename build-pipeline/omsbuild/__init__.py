"""Smart build pipeline for the Northwind Order Management System (OSGi/Tycho).

The product's real dependency structure is expressed in OSGi metadata rather
than in Maven POMs, so this package parses the manifests and feature
definitions, derives a directed dependency graph, and uses it to drive two
build scenarios:

* ``full``    -- build every module in dependency order
* ``changed`` -- detect modified modules, resolve their full impact closure,
                 and build only those, still in dependency order

Entry point: :func:`omsbuild.cli.main`.
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["cli", "changes", "graph", "model", "plan", "render", "runner", "scan"]
