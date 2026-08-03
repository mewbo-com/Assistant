"""Name-to-directory resolution and the git-remote registry.

Projects, workspaces, worktrees and registered repositories: everything that
answers "where on disk does this name live" and "which remote does it point
at".

This ``__init__`` is deliberately EMPTY of code. Importing ``project_catalog``
already pulls ``project_store`` and ``worktree``, each of which calls
``get_logger()`` in its module body and so reaches the config loader at import
time; a re-export here would make importing ANY module in the package execute
all eight. ``repository_store`` also defers its Mongo driver for the reason
given in ``secrets/__init__.py``, and a facade would undo that too.
"""
