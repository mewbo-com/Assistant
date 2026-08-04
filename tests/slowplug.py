"""Opt-in pytest plugin: per-test watchdog + optional RSS sampling.

**Never loaded by a plain ``pytest`` invocation.** This module is only
imported when explicitly named on the command line — ``pytest -p slowplug``
(``tests/`` is already on ``sys.path`` under pytest, so the bare module name
resolves). That is what makes it inert by default: with no ``-p slowplug``,
the interpreter never even imports this file, so an ordinary run pays
nothing for it — no import cost, no watchdog timer, no RSS sampling.

Config knobs, both optional. They are env vars rather than pytest CLI flags
because a custom ``addoption`` needs a ``pyproject.toml``/``conftest.py``
registration this module deliberately avoids owning:

  ``SLOWPLUG_WATCHDOG``  seconds before ``faulthandler`` dumps a stack for a
                          test still running (default 30). This is what turns
                          "the suite hangs somewhere" into a named test with a
                          traceback.
  ``SLOWPLUG_RSS_LOG``   path to append a wall-time + RSS TSV row per test.
                          Unset means RSS sampling is skipped entirely — it is
                          not free (two ``/proc/self/status`` reads per test),
                          so it stays opt-in even under ``-p slowplug``.

Trap this plugin exists to remember: ``pytest_runtest_protocol`` is a
``firstresult`` hook. An implementation that returns ``None`` runs BEFORE the
default protocol and wraps nothing — every timing/RSS delta such a version
records reads zero, silently, with no error raised anywhere. It must be a
``hookwrapper`` that ``yield``s exactly once, which is why
``SlowplugWatchdog.pytest_runtest_protocol`` below is a generator method, not
a plain callable.

py-spy note, found the hard way: an attach (``py-spy dump -p <pid>``) is
refused here because yama's ``ptrace_scope=1`` only allows a process to trace
its own descendants. The parent form works because py-spy then launches the
target itself and is its parent:

    py-spy record -o profile.svg -- python -m pytest tests/test_slow_thing.py

Not in scope: making this run in CI. It is a tool for someone investigating a
stall or a memory climb, not a gate — a wall-clock assertion on a shared
runner is flaky by construction and teaches people to ignore red.
"""

from __future__ import annotations

import faulthandler
import os
import sys
import time
from collections.abc import Generator
from dataclasses import dataclass, field

import pytest


@dataclass
class SlowplugWatchdog:
    """Per-test watchdog + optional RSS sampling; one instance owns all state.

    Reading ``SLOWPLUG_*`` once at construction (rather than on every hook
    call) keeps the hot path — one hook invocation per test — to arming and
    disarming a timer, plus two cheap ``/proc`` reads when RSS logging is on.
    """

    watchdog_seconds: float = field(
        default_factory=lambda: float(os.environ.get("SLOWPLUG_WATCHDOG", "30"))
    )
    rss_log_path: str = field(
        default_factory=lambda: os.environ.get("SLOWPLUG_RSS_LOG", "")
    )

    def pytest_configure(self, config: pytest.Config) -> None:
        faulthandler.enable(file=sys.stderr)

    def pytest_runtest_protocol(
        self, item: pytest.Item, nextitem: pytest.Item | None
    ) -> Generator[None, object, None]:
        """Arm the watchdog for one test; sample RSS around it if configured.

        Must be driven as a hookwrapper (see module docstring) — this method
        is a plain generator so the real hookimpl lives on the trampoline
        below, which is the attribute pytest's plugin loader actually scans.
        """
        start = time.monotonic()
        before_rss = self._proc_status_kb("VmRSS:")
        before_hwm = self._proc_status_kb("VmHWM:")
        faulthandler.dump_traceback_later(self.watchdog_seconds, repeat=True, exit=False)
        try:
            yield
        finally:
            faulthandler.cancel_dump_traceback_later()
            if self.rss_log_path:
                self._append_rss_row(item.nodeid, start, before_rss, before_hwm)

    def _append_rss_row(
        self, nodeid: str, start: float, before_rss: int, before_hwm: int
    ) -> None:
        elapsed = time.monotonic() - start
        after_rss = self._proc_status_kb("VmRSS:")
        after_hwm = self._proc_status_kb("VmHWM:")
        with open(self.rss_log_path, "a") as fh:
            fh.write(
                f"{elapsed:.3f}\t{before_rss}\t{after_rss}\t"
                f"{after_rss - before_rss}\t{after_hwm - before_hwm}\t"
                f"{after_hwm}\t{nodeid}\n"
            )

    @staticmethod
    def _proc_status_kb(key: str) -> int:
        try:
            with open("/proc/self/status") as fh:
                for line in fh:
                    if line.startswith(key):
                        return int(line.split()[1])
        except OSError:
            pass
        return 0


# Pytest's `-p <module>` loader registers the imported MODULE itself as the
# plugin object and scans its top-level attributes for hook-named callables
# (https://docs.pytest.org/en/stable/how-to/writing_plugins.html) — it does
# not know how to discover hooks on an arbitrary instance. These two
# module-level functions are that required registration seam: thin
# trampolines onto the one watchdog instance, not a second copy of the logic.
_watchdog = SlowplugWatchdog()


def pytest_configure(config: pytest.Config) -> None:
    _watchdog.pytest_configure(config)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(
    item: pytest.Item, nextitem: pytest.Item | None
) -> Generator[None, object, None]:
    yield from _watchdog.pytest_runtest_protocol(item, nextitem)
