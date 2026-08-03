"""API-key storage: the base store and its Mongo driver.

An isolated pair with one consumer outside core and no dependents inside it,
which is what makes it the smallest independently reviewable unit in the
package.

This ``__init__`` is deliberately EMPTY of code, and here the reason is
mechanical rather than stylistic. ``key_store`` reaches ``key_store_mongo``
through a function-local import, because the driver subclasses the base — and
that same deferral is what keeps a ``storage.driver=json`` deployment from
importing ``pymongo`` at all. A convenience re-export in this file would import
both modules the moment anything touched the package, defeating the deferral
outright.
"""
