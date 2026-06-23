"""Adapter registry — AUTO-DISCOVERED from the source modules in this package.

Each source adapter module (e.g. ``varsha.py``) ends with ``ADAPTER = <Source>Adapter()``.
Dropping such a file into this package registers it automatically — there is NO manual list
to keep in sync. Framework modules (``base``, ``tokens``, ``selftest``) and templates
(``_*``) are skipped. ``REGISTRY`` maps ``adapter.source`` -> the adapter instance.
"""

from __future__ import annotations

import importlib
import pkgutil

from stone_pipeline.adapters.base import AdapterBase

_SKIP = {"base", "tokens", "selftest"}


def _discover() -> dict[str, AdapterBase]:
    registry: dict[str, AdapterBase] = {}
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_") or info.name in _SKIP:
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        adapter = getattr(module, "ADAPTER", None)
        if isinstance(adapter, AdapterBase) and adapter.source:
            registry[adapter.source] = adapter
    return registry


REGISTRY: dict[str, AdapterBase] = _discover()
