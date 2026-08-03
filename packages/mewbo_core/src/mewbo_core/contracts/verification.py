#!/usr/bin/env python3
"""Verifier-gated completion — a ground-truth check at the natural-completion seam.

A ``CommandVerification`` is a caller-declared, data-owned check that must pass
before an agent's *claimed* completion is accepted. It mirrors the trigger
domain (``triggers/spec.py``): a discriminated union whose single member owns
its own validators + verdict logic, parsed through ONE total ``from_value``
seam that never raises (a garbage/empty spec leaves the gate inert). The model
authors no verification LOGIC — it picks a ``kind`` and fills argv; the same
guardrail the trigger subsystem draws ("behaviour is DECLARED via typed kind
classes, NEVER an LLM-authored script").

Models never import I/O: :meth:`CommandVerification.interpret` turns an
already-collected :class:`VerifierResult` into a pass/fail
:class:`VerifierOutcome` as a pure method; the subprocess itself lives behind
the :class:`VerifierRunner` Protocol (:class:`CommandVerifierRunner` concrete),
injected into the loop so a test drives the gate with a recording fake and
never spawns a real process.

``VerifierResult``/``VerifierOutcome`` are frozen dataclasses, not Pydantic:
they are hot in-process runtime values that cross no trust boundary — the
documented carve-out from the "Pydantic at every trust boundary" rule.

This module's ONLY module-top ``mewbo_core`` import is ``contracts.defaults``,
which itself imports nothing (``_scrubbed_env``, ``get_config_value`` and
``shell_preexec_scope`` are pulled in lazily inside
:meth:`CommandVerifierRunner.run`, each argued at its call site). That restraint
is load-bearing: ``config.py`` reads this module's default constant, so any
other top-level core import here would reopen a
config → verification → hooks → classes → config cycle.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, ClassVar, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

# Default ceiling (seconds) on a single verifier subprocess. Declared in
# ``contracts/defaults.py`` so ``AgentConfig`` (``verification_timeout_s``) can
# read it without importing this module, and re-imported here so config and code
# still share ONE source of truth and every existing
# ``from mewbo_core.contracts.verification import DEFAULT_VERIFICATION_TIMEOUT``
# keeps resolving. ``contracts.defaults`` imports nothing, so this does not
# weaken the zero-core-import property stated above.
from mewbo_core.contracts.defaults import DEFAULT_VERIFICATION_TIMEOUT

# Bounded tail (chars) of each of stderr/stdout folded into the feedback
# injected back into the model's context on a failed check — enough to ground
# a fix, capped so a chatty command can't blow the context window.
_FEEDBACK_TAIL_CAP = 4000


@dataclass(frozen=True)
class VerifierResult:
    """Raw outcome of ONE verifier execution — an in-process runtime value.

    ``error`` is set only when the runner could not run the command at all
    (a missing executable, a bad argv) — distinct from a command that ran and
    exited non-zero (``exit_code != 0``, ``error is None``).
    """

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    error: str | None = None


@dataclass(frozen=True)
class VerifierOutcome:
    """Interpreted verdict of a verifier run — pass/fail + bounded feedback."""

    passed: bool
    feedback: str
    exit_code: int
    timed_out: bool


class CommandVerification(BaseModel):
    """Run a command; a clean exit (0, no timeout, no runner error) is a pass.

    The one verification ``kind`` today. Owns its own argv validator and its
    ``interpret`` verdict — there is deliberately no service-side ``if kind ==``
    switch, exactly as ``TriggerSpec`` subclasses own their behaviour.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["command"] = "command"
    argv: list[str]
    cwd: str | None = None
    # Per-spec ceiling; clamped by ``agent.verification_timeout_s`` at run.
    timeout_s: float = DEFAULT_VERIFICATION_TIMEOUT

    @field_validator("argv")
    @classmethod
    def _non_empty_argv(cls, value: list[str]) -> list[str]:
        """Require a non-empty list of non-empty strings (argv, never a shell)."""
        if not value or not all(isinstance(a, str) and a for a in value):
            raise ValueError("argv must be a non-empty list of non-empty strings")
        return value

    def interpret(self, result: VerifierResult) -> VerifierOutcome:
        """Fold a raw ``VerifierResult`` into a pass/fail verdict + feedback.

        No I/O — the runner already ran the process. A pass requires a clean
        exit AND no timeout AND no runner-level error. ``feedback`` is the
        bounded stderr-then-stdout tail: the ONE grounded artefact injected
        back into the model's context on a failure.
        """
        passed = result.exit_code == 0 and not result.timed_out and result.error is None
        return VerifierOutcome(
            passed=passed,
            feedback=self._feedback(result),
            exit_code=result.exit_code,
            timed_out=result.timed_out,
        )

    @staticmethod
    def inactive_reason(*, enabled: bool, capability_mode: str) -> str | None:
        """Why a supplied spec's gate is INERT for these conditions, else ``None``.

        The ONE two-gate predicate over external state: the master switch AND
        an act-capable ``capability_mode`` (∈ {execute, all}). ``None`` means
        the gate WOULD run. The loop's ``_verification_active`` and
        spawn_agent's no-silent-drop note both read this so they cannot drift.
        Asking presupposes a spec exists (``verification is not None`` is the
        caller's own gate). The returned string is surfaced verbatim in the
        spawn response + event so a declared-but-dropped verification is never
        an invisible null (the freshness-null trap).
        """
        if not enabled:
            return "specified_but_inactive (verification_enabled=false)"
        if capability_mode not in ("execute", "all"):
            return f"specified_but_inactive (capability_mode={capability_mode})"
        return None

    @classmethod
    def gate_active(cls, *, enabled: bool, capability_mode: str) -> bool:
        """Whether a supplied spec's gate would RUN — ``inactive_reason`` is ``None``."""
        return cls.inactive_reason(enabled=enabled, capability_mode=capability_mode) is None

    @staticmethod
    def _feedback(result: VerifierResult) -> str:
        """Bounded, grounded rendering of a run's output (stderr then stdout)."""
        if result.error is not None:
            return f"verifier could not run: {result.error}"[:_FEEDBACK_TAIL_CAP]
        parts: list[str] = []
        if result.timed_out:
            parts.append("verifier timed out")
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        if stderr:
            parts.append(f"stderr:\n{stderr[-_FEEDBACK_TAIL_CAP:]}")
        if stdout:
            parts.append(f"stdout:\n{stdout[-_FEEDBACK_TAIL_CAP:]}")
        if not parts:
            parts.append(f"exit code {result.exit_code}")
        return "\n".join(parts)

    # Lazily built on first ``parse()`` and cached — mirrors ``TriggerSpec``.
    _adapter: ClassVar[TypeAdapter | None] = None

    @classmethod
    def parse(cls, data: Mapping[str, object]) -> CommandVerification:
        """Parse a raw mapping into its concrete kind via the ONE union adapter."""
        if CommandVerification._adapter is None:
            CommandVerification._adapter = TypeAdapter(VerificationUnion)
        return CommandVerification._adapter.validate_python(data)

    @classmethod
    def from_value(cls, value: object) -> CommandVerification | None:
        """TOTAL parse: a non-mapping / malformed / empty-argv spec → ``None``.

        Never raises — a bad spec leaves the gate INERT (``None`` means "no
        verification"), mirroring ``DelegationContract.from_value`` /
        ``RetryPolicy.from_value``. This is an opt-in ceiling, not a
        correctness contract, so a typo degrades to no-gate, never a failed
        spawn. Exactly one ``kind`` exists, so an omitted/blank discriminator
        on an otherwise-well-formed mapping is unambiguous and defaulted; a
        wrong non-empty ``kind`` still fails the union → ``None``.
        """
        if not isinstance(value, Mapping):
            return None
        data = dict(value)
        if not data.get("kind"):
            data["kind"] = "command"
        try:
            return cls.parse(data)
        except Exception:
            return None


VerificationUnion = Annotated[CommandVerification, Field(discriminator="kind")]


class VerifierRunner(Protocol):
    """Executes a verification spec, returning its raw result. Never raises."""

    async def run(self, spec: CommandVerification, *, cwd: str | None) -> VerifierResult:
        """Execute ``spec`` (falling back to ``cwd``) and return its raw result."""
        ...


@dataclass(frozen=True)
class CommandVerifierRunner:
    """Concrete ``VerifierRunner`` — runs argv in a subprocess off the loop thread.

    argv list, NEVER ``shell=True`` (no shell-injection surface). Runs under a
    scrubbed env (``hooks._scrubbed_env`` — the same minimal PATH/HOME/LANG a
    command hook gets, so a verifier can't read the process's LLM keys / DB
    URIs). Total: a timeout or an OS error becomes a failed ``VerifierResult``,
    never an exception into the loop.

    **Both halves of the spawn are model-supplied, so both are bounded.**
    ``argv`` AND ``cwd`` arrive from the model through ``spawn_agent``'s
    ``checks[]``, which makes this a model-to-process seam and puts it under the
    same rule as every other one we own: *the scope must never derive from a
    model argument*.

    * **Filesystem confinement** comes from
      :func:`mewbo_core.workspaces.workspace.shell_preexec_scope` — the EXISTING
      down-only registration seam ``mewbo_tools.integration.landlock`` pushes its
      Landlock factory into at its own import time. Calling it applies the ONE
      ``ShellScope`` deny-list the shell tool, the pipeline runner and the wiki
      git executor already share, and adds NO package edge: core keeps importing
      only core, and a core-only install with ``mewbo_tools`` absent simply gets
      today's unconfined spawn rather than a failure. The alternative — a scope
      DI'd in by whoever constructs the runner — was rejected because the sole
      production constructor (``ToolUseLoop.__init__``) has no scope object to
      hand over and could only reach one by importing ``mewbo_tools`` itself,
      i.e. by adding the 13th reach-up root ``CLAUDE.md`` refuses.
    * **The ``cwd`` bound is arithmetic, not a kernel control**, so it holds even
      where Landlock does not (no registered factory, an old kernel, a
      ``shell_sandbox=false`` deployment). ``spec.cwd`` must resolve UNDER an
      allowed root; the roots are the loop-published ``cwd`` this method is
      called with plus any ``allowed_roots`` the constructor was given, never
      anything the model wrote. A ``spec.cwd`` outside them is REFUSED (a
      ``VerifierResult`` carrying ``error``) rather than silently rewritten, so
      the failure names itself in the model's own feedback; and with no allowed
      root to check against it is refused too, per root ``CLAUDE.md``'s "a filter
      that cannot be applied must refuse, never fall back to everything".
    """

    #: Extra roots a ``spec.cwd`` may resolve under, beyond the loop-published
    #: ``cwd`` passed to :meth:`run`. Empty in production today; the DI seam
    #: exists so a caller that legitimately owns more than one workspace can say
    #: so without this class learning to look one up.
    allowed_roots: tuple[str, ...] = ()

    async def run(self, spec: CommandVerification, *, cwd: str | None) -> VerifierResult:
        """Run ``spec.argv`` in a subprocess off-thread; total (never raises).

        Cost: ``O(1)`` here plus whatever the verifier command itself costs,
        bounded by ``min(spec.timeout_s, agent.verification_timeout_s)``.
        """
        # Lazy imports: this module is imported by ``config.py`` for its default
        # constant, so a top-level core import would reopen an import cycle.
        # ``workspaces.workspace`` joins them under the SAME argument the
        # `contracts/` CLAUDE.md demands per site: it is reached only at call
        # time, long after every module body has run, and its own import-time
        # side effects are a `PLAN_DIR_ROOT` constant and two `ContextVar`
        # constructions — nothing that can re-enter this module or config.
        from mewbo_core.config import get_config_value
        from mewbo_core.hooks import _scrubbed_env
        from mewbo_core.workspaces.workspace import shell_preexec_scope

        ceiling = float(
            get_config_value(
                "agent", "verification_timeout_s", default=DEFAULT_VERIFICATION_TIMEOUT
            )
        )
        timeout = min(spec.timeout_s, ceiling)
        if timeout <= 0:
            timeout = ceiling
        run_cwd, refusal = self._resolve_cwd(spec, cwd)
        if refusal is not None:
            return VerifierResult(
                exit_code=-1, stdout="", stderr="", timed_out=False, error=refusal
            )
        try:
            # The ruleset is built HERE and only ``restrict_self`` runs in the
            # forked child, exactly as ``ShellSession`` composes it. An
            # unregistered factory yields ``None``, which ``subprocess.run``
            # takes as "no hook" — today's behaviour, never a failure.
            with shell_preexec_scope(run_cwd) as restrict_child:
                completed = await asyncio.to_thread(
                    subprocess.run,
                    spec.argv,
                    cwd=run_cwd,
                    env=_scrubbed_env(),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    preexec_fn=restrict_child,
                )
        except subprocess.TimeoutExpired as exc:
            return VerifierResult(
                exit_code=-1,
                stdout=_as_text(exc.stdout),
                stderr=_as_text(exc.stderr),
                timed_out=True,
            )
        # ``SubprocessError`` joins the other two because a ``preexec_fn`` that
        # RAISES comes back as ``SubprocessError("Exception occurred in
        # preexec_fn.")``, which is not an OSError — letting it escape would
        # break this runner's "never raises into the loop" contract.
        # ``TimeoutExpired`` is itself a ``SubprocessError``, hence the order.
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            # FileNotFoundError (argv[0] missing) is an OSError subclass.
            return VerifierResult(
                exit_code=-1, stdout="", stderr="", timed_out=False, error=str(exc)
            )
        return VerifierResult(
            exit_code=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            timed_out=False,
        )

    def _resolve_cwd(
        self, spec: CommandVerification, cwd: str | None
    ) -> tuple[str | None, str | None]:
        """Bound the model's ``spec.cwd``: ``(run_cwd, None)`` or ``(None, refusal)``.

        No ``spec.cwd`` means the caller's own ``cwd`` is used verbatim — it is
        the loop's authoritative working directory, not model text. A supplied
        one is resolved through ``realpath`` (so a symlink cannot step out) and
        must sit at or beneath an allowed root.
        """
        if not spec.cwd:
            return cwd, None
        roots = tuple(
            os.path.realpath(root) for root in (*self.allowed_roots, cwd or "") if root
        )
        if not roots:
            return None, (
                "refusing the verifier cwd: no allowed root to check it against "
                "(the caller published none)"
            )
        candidate = os.path.realpath(spec.cwd)
        if any(candidate == root or candidate.startswith(root + os.sep) for root in roots):
            return candidate, None
        return None, (
            f"refusing the verifier cwd {spec.cwd!r}: outside the allowed roots "
            f"({', '.join(roots)})"
        )


def _as_text(value: object) -> str:
    """Coerce ``TimeoutExpired.stdout``/``stderr`` (str | bytes | None) to str."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


__all__ = [
    "DEFAULT_VERIFICATION_TIMEOUT",
    "CommandVerification",
    "VerificationUnion",
    "VerifierResult",
    "VerifierOutcome",
    "VerifierRunner",
    "CommandVerifierRunner",
]
