"""The closed delegation subsystem: admission, dispatch, and agent identity.

These modules reference each other's types and are close to meaningless apart —
they are the type-level strongly-connected component of the package.

This ``__init__`` is deliberately EMPTY of code. ``hypervisor`` reaches
``attestation`` and ``spawn_agent`` only under ``if TYPE_CHECKING:``, which is
what keeps the runtime graph acyclic while both partners import the
hypervisor's types for real. A convenience re-export would make importing any
one of the six import all six, converting two edges that deliberately never
execute into genuine import-order coupling — the exact failure the guards
exist to prevent.
"""
