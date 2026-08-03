"""Everything that produces or admits a callable surface for the model.

The registry itself, the session-scoped and client-side tool families, skills,
plugins, and the individual built-in tools. All of it sits below the loop and
above the spine.

This ``__init__`` is deliberately EMPTY of code, and this package is the one
where a facade would do the most damage. ``config`` reaches ``plugins`` through
a function-local import that is load-bearing — it is what keeps ``config``, the
module nearly all of core imports, from importing a consumer at module top. A
re-export here would mean any import of any tooling module pulls ``plugins``
in, and ``tool_registry`` constructs its ``ToolRegistryCache`` singleton at
module scope besides.
"""
