"""Modules that import NOTHING from the rest of ``mewbo_core`` at module top.

That shared property is the point of the package, not a coincidence of what
happens to live here. ``config.py`` imports its field defaults from these
modules, so anything under ``contracts/`` that grew a module-top core import
would close a cycle through config — and config is imported by essentially
everything, so the failure would surface as an ``ImportError`` somewhere far
from the edit.

This ``__init__`` is deliberately EMPTY of code. A convenience re-export here
would make importing any one module in the package execute all of them, which
changes when and in what order import-time side effects fire elsewhere in core
— a difference invisible in a diff.
"""
