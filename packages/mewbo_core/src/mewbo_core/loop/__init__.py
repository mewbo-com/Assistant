"""The drivers — everything that RUNS a session rather than storing one.

The tool-use loop itself, the orchestrator around it, the session runtime at
the top of the dependency graph, task decomposition, plan mode, and structured
response assembly.

This ``__init__`` is deliberately EMPTY of code. ``structured_response``,
``structured_synthesis`` and the planning modules render prompt templates off
disk into module constants at import, and ``task_master`` mutates a
process-global ``warnings`` filter in its module body. A re-export would fire
all of that for anything that touched the package, and this package is imported
by the product surfaces.
"""
