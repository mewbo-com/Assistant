"""The durable record of a session, and every pure projection over it.

Storage, the event bus, provenance, transcript assembly, sharing,
notifications, attachments, token accounting and compaction. Deliberately does
NOT include ``session_runtime``: that module DRIVES a session rather than
storing one, sits at the top of the dependency graph, and reaches this package
through ``task_master`` -> ``orchestrator``. Filing the two "session" modules
together by name is precisely the grouping that would make this package and
``loop/`` import each other.

This ``__init__`` is deliberately EMPTY of code. ``session_store`` defers its
Mongo driver (see ``secrets/__init__.py``), and ``title_generator`` renders a
prompt template off disk into a module constant at import — a re-export would
fire both for anything that touched the package.
"""
