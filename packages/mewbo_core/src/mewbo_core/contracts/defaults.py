#!/usr/bin/env python3
"""Default constants shared between ``config.py`` and the modules that use them.

``config.py`` needs these values to declare its field defaults, and the
behaviour modules need them to fall back on when no config is loaded. Defining
them in either place makes the other import it: config importing
``llm_resilience`` is what pinned the whole LLM stack to the package root, and
the reverse would make the retry ladder unusable without a config.

So they live in a module that imports nothing and can be imported by anything.
``llm_resilience`` and ``verification`` re-import them, so
``from mewbo_core.llm.llm_resilience import DEFAULT_TIMEOUT`` still resolves and
every consumer still reads the constant from the module whose behaviour it
describes.

**This module must never gain an import from elsewhere in ``mewbo_core``.**
One would cycle through config, and config is imported by nearly everything.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# LLM retry / fallback — the behaviour lives in ``llm_resilience.py``
# ---------------------------------------------------------------------------

# Total ceiling for ONE attempt. This is the loosest of the four bounds by
# design: it is a backstop for a wedge, NOT the bound that should normally fire.
# ``DEFAULT_STREAM_IDLE_TIMEOUT`` is the bound that does the real work, because
# duration alone cannot tell a healthy long generation from a dead connection —
# and a total ceiling tight enough to catch the dead one kills the healthy one.
# The ordering the bounds must keep, loosest to tightest:
#   request (transport) > liveness > this (total) > stream idle
#   > (first token, if enabled)
# Invert any pair and the tighter bound silently becomes the real ceiling while
# the knob an operator can see stops meaning anything. Liveness sits ABOVE this
# one deliberately — it exists for the wedge the attempt cap cannot catch, so it
# must only ever speak after the cap has failed to. Raising this without raising
# liveness collides the two, which `test_liveness_exceeds_the_attempt_cap`
# refuses.
DEFAULT_TIMEOUT = 300.0
# Handed to the LiteLLM client as its HTTP timeout. The loop streams, so this is
# the maximum gap BETWEEN chunks rather than a ceiling on total call duration —
# ``DEFAULT_TIMEOUT`` above is the total ceiling. Kept beside it so the two are
# read together and nobody tunes one believing it bounds the other.
#
# It is a BACKSTOP, and must stay well above every in-process bound below, or it
# silently becomes the real ceiling and the knobs an operator can actually see
# stop meaning anything. At 60 it undercut BOTH of them: it capped the total
# ceiling wherever that was raised past 60, and it contradicted
# ``DEFAULT_FIRST_TOKEN_TIMEOUT``'s deliberate "0 = wait as long as the total
# ceiling allows" — a model that legitimately takes minutes to emit its first
# token was killed at 60s by the transport, with the guard meant to govern that
# wait switched off. The symptom is indistinguishable from a provider outage:
# a timeout at exactly the transport bound, on a healthy model.
DEFAULT_REQUEST_TIMEOUT = 900.0
# How long one outstanding provider read may go silent before the loop emits
# ``llm_call_stalled``. Deliberately longer than ``DEFAULT_TIMEOUT``: this leg
# exists for the case ``asyncio.wait_for`` CANNOT catch — a read wedged below the
# event loop never reaches a cancellation point, so the attempt cap never fires.
DEFAULT_LLM_CALL_LIVENESS_S = 600.0
# One try + one retry, then advance to the next model. A 3rd same-model attempt
# rarely recovers a transient fault that survived a backed-off retry; the run is
# better served by escalating to a healthy model (which is then pinned — see
# ``RetryStrategy._pinned_model``).
DEFAULT_PRIMARY_RETRIES = 2
DEFAULT_FALLBACK_RETRIES = 1
# Ceiling on the wait for the FIRST chunk of a streamed call. 0 means "use the
# total ceiling", which is the default:
# a large prompt on an extended-thinking model can legitimately take minutes to
# emit its first token, so a tighter value here manufactures failures rather than
# catching them. Raise the total ceiling to allow a slower first token; set this
# BELOW the total ceiling only when a deployment knows its own first-token
# latency.
DEFAULT_FIRST_TOKEN_TIMEOUT = 0.0
# Maximum gap BETWEEN chunks once a stream has started producing. This is the
# bound a single total ceiling cannot express: a provider that returns 200 and
# then goes silent is otherwise unbounded until the total ceiling, and the loop
# cannot tell it apart from a slow-but-healthy generation. 0 disables it.
#
# Sized against what a stall actually looks like, not against what a turn should
# cost. A reasoning model can hold a stream open through minutes of internal work
# before the next visible chunk, so a tight value here manufactures failures on
# exactly the long generations this bound exists to protect. Comparable harnesses
# sit in the 240-300s range for the same reason; 60 was the outlier by 4-5x.
DEFAULT_STREAM_IDLE_TIMEOUT = 240.0
DEFAULT_BACKOFF_BASE = 1.0
DEFAULT_BACKOFF_CAP = 60.0
DEFAULT_RETRY_AFTER_CAP = 60.0
# 0 disables the wall-clock terminator. This MUST leave room for the primary rung
# (``DEFAULT_TIMEOUT * DEFAULT_PRIMARY_RETRIES``) plus at least one fallback attempt,
# or the ladder below rung 1 is unreachable and cross-model fallback silently never
# happens. At 240 the primary rung alone consumed the whole budget — every escalation
# died on the deadline before the fallback model was invoked. Raising this does NOT
# lengthen a single-model run: each attempt is still capped by ``llm_call_timeout``,
# so the deadline only ever binds a multi-rung chain.
# Re-derived from the per-attempt ceiling above, which is the only way this stays
# correct: it must cover the primary rung (``DEFAULT_TIMEOUT`` x
# ``DEFAULT_PRIMARY_RETRIES``) PLUS a fallback attempt for each rung a deployment
# configures, or the ladder silently stops one rung short of where it was
# configured to reach. Sized here for a primary plus three fallbacks:
#   300 x 2  +  300 x 3  =  1500
# Raising ``DEFAULT_TIMEOUT`` without raising this re-creates the failure it was
# raised from — the config validator warns at load, and that warning is the one
# signal that this pair has drifted apart.
DEFAULT_TURN_DEADLINE = 1500.0
DEFAULT_BUDGET_CAPACITY = 24.0
# A 429 gets its own, far smaller bucket than a transient fault. Retrying a
# server error is a bet that the server recovers in seconds and usually pays;
# retrying a throttle is a bet against a rate window the provider controls, and
# every attempt re-sends the whole prompt. Two retries then leave for a model
# that draws on a DIFFERENT pool, rather than spending the turn deadline here.
DEFAULT_RATE_LIMIT_BUDGET_CAPACITY = 4.0
DEFAULT_CB_THRESHOLD = 3
DEFAULT_CB_COOLDOWN = 30.0
DEFAULT_DOOM_LOOP_THRESHOLD = 3
# Consecutive non-write tool-execution steps before the write-progress signal
# fires telemetry for a write-capable agent that keeps exploring instead of
# acting. The event repeats every ``event_interval`` steps thereafter, up to
# ``max_events``.
DEFAULT_WRITE_PROGRESS_THRESHOLD = 25
DEFAULT_WRITE_PROGRESS_EVENT_INTERVAL = 10
DEFAULT_WRITE_PROGRESS_MAX_EVENTS = 2

# ---------------------------------------------------------------------------
# Verifier-gated completion — the behaviour lives in ``verification.py``
# ---------------------------------------------------------------------------

# Default ceiling (seconds) on a single verifier subprocess. The tunable lives
# in ``AgentConfig`` (``verification_timeout_s``).
DEFAULT_VERIFICATION_TIMEOUT = 60.0
