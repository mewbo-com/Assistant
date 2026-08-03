"""Operator-owned safety plane: a tool-call gate and a session observer.

See ``CLAUDE.md`` in this directory for the doctrine. This package holds no
code at import time by design — every subpackage ``__init__`` stays empty so
importing one module never executes the others.
"""
