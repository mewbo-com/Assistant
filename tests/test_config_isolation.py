"""The suite's config isolation, pinned as a contract rather than a habit.

Two properties keep one test module's config out of the next one's, and both
were believed rather than checked. Neither is expensive to assert, and the cost
of either silently lapsing is paid somewhere far away: a leaked scope setting
reaches the shell sandbox's deny set, where the symptom is a test failing in a
module that did nothing wrong.

1. **An override never survives a test.** The root ``conftest.py`` runs
   ``reset_config()`` at the setup AND the teardown of every test, via the
   autouse ``app_config_file`` fixture, and ``reset_config`` clears the override
   mapping outright. A module that calls ``set_config_override`` and never
   resets it is therefore harmless — but only for as long as that fixture keeps
   doing both halves, which is what the first test here pins.
2. **The config directory is the suite's own.** The pin exists so the chain
   cannot walk up to a developer's live, secret-bearing ``configs/app.json``.
   Written with ``setdefault`` it deferred to an inherited value, which made it
   inert in precisely the environment it was written to defend against.
"""

from __future__ import annotations

import os
from pathlib import Path

from mewbo_core import config as config_module
from mewbo_core.config import get_config_value, set_config_override


class TestAnOverrideNeverOutlivesItsTest:
    """The autouse reset is load-bearing, so it is asserted from both sides."""

    def test_this_test_starts_from_a_clean_override_set(self):
        assert config_module._APP_CONFIG_OVERRIDE == {}, (
            "an override reached this test from somewhere else; the autouse "
            "reset in the root conftest is no longer running on both halves"
        )

    def test_an_override_set_here_is_visible_here(self):
        # Deliberately left un-reset: the point is that the fixture cleans up
        # after a module that does not, which is the shape eleven modules have.
        set_config_override({"agent": {"shell_denied_paths": ["/leaked"]}})
        assert get_config_value("agent", "shell_denied_paths") == ["/leaked"]

    def test_the_previous_test_did_not_leak_into_this_one(self):
        assert config_module._APP_CONFIG_OVERRIDE == {}
        assert get_config_value("agent", "shell_denied_paths", default=[]) == []


class TestTheConfigDirectoryIsTheSuitesOwn:
    """The pin must beat an inherited value, not defer to one."""

    def test_the_pinned_directory_is_the_throwaway_one(self):
        pinned = os.environ.get("MEWBO_CONFIG_DIR")
        assert pinned, "the suite must pin a config directory for its subprocesses"
        assert Path(pinned).is_dir()

    def test_the_pin_is_not_a_developers_live_checkout(self):
        """The failure this guards against is silent and reads as a pass.

        A suite resolving a real deployment's config asserts against whatever
        that operator happens to have configured, and reads their secrets to do
        it. Both look exactly like a healthy run.
        """
        pinned = Path(os.environ["MEWBO_CONFIG_DIR"]).resolve()
        repo_configs = (Path(__file__).resolve().parents[1] / "configs").resolve()
        assert pinned != repo_configs, (
            "the suite is pointed at the checkout's own configs directory; an "
            "inherited MEWBO_CONFIG_DIR has beaten the pin"
        )
