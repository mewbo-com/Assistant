"""Construct-family fixture: every shape of Python definition the graph models.

Every symbol here is named for the family it represents, so a failure names the
capture pattern that regressed. `test_graph_coverage.py` also runs this file
through the stdlib `ast` and demands parity, which is what catches a family
nobody thought to enumerate below.
"""
import functools


# ── plain module level ────────────────────────────────────────────────────────
def module_fn():
    """Docstring survives the family split."""


async def module_async_fn():
    pass


class ModuleClass:
    def plain_method(self):
        pass

    async def async_method(self):
        pass


# ── decorated ─────────────────────────────────────────────────────────────────
@functools.cache
def decorated_fn():
    """A decorated def parses as (decorated_definition (function_definition ...))."""


@functools.cache
@functools.wraps(module_fn)
def multi_decorated_fn():
    pass


@functools.total_ordering
class DecoratedClass:
    def __lt__(self, other):
        return False

    def __eq__(self, other):
        return False

    @property
    def decorated_property(self):
        return 1

    @staticmethod
    def decorated_staticmethod():
        pass

    @classmethod
    def decorated_classmethod(cls):
        pass


# ── nested ────────────────────────────────────────────────────────────────────
def outer_fn():
    def nested_fn():
        pass

    class NestedInFunction:
        def method_of_nested_class(self):
            pass

    return nested_fn, NestedInFunction


class OuterClass:
    class NestedClass:
        def nested_class_method(self):
            pass

    def method_holding_a_fn(self):
        def fn_inside_method():
            pass

        return fn_inside_method


# ── conditional / guarded ─────────────────────────────────────────────────────
if True:
    def conditional_fn():
        pass

    class ConditionalClass:
        pass


class ClassWithGuardedBody:
    if True:
        def conditional_method(self):
            pass
