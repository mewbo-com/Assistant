#!/usr/bin/env python3
"""ContextMeter — tokens → context-window % and tokens → cost.

One atomic class that turns a live token count into the two honest numbers the
status bar shows:

- **context-window %** — ``used / total``. ``total`` comes from
  :func:`get_model_max_input_tokens` — the one place window resolution is
  implemented (user override under ``token_budget.model_context_windows`` →
  LiteLLM's catalogue → ``token_budget.default_context_window``). ``estimated``
  is ``True`` only on the last leg — flagged with a ``~`` prefix so the user
  knows it is not the real window for this model.
- **cost ($)** — derived from input/output tokens via LiteLLM's per-token price
  table (the same pricing the proxy bills on). When LiteLLM has no price for the
  model we degrade *honestly* to ``None`` (the status bar renders ``—``) rather
  than inventing a number.

KISS/DRY: pricing comes from LiteLLM (already a project dependency for every LLM
call); window resolution comes from the one resolver ``token_budget.py`` already
implements — this module used to re-read a ``llm.*`` config section that has no
matching ``LLMConfig`` field, which made the meter silently hardcode 128000 for
every model. Reusing the resolver both fixes that and keeps a config-key rename
in one place.
"""

from __future__ import annotations

from dataclasses import dataclass

from mewbo_core.session.token_budget import (
    _litellm_max_input_tokens,
    _load_context_overrides,
    _strip_provider_prefix,
    get_model_max_input_tokens,
)


@dataclass(frozen=True)
class ContextUsage:
    """The computed context-window usage for one render.

    ``estimated`` is ``True`` when ``total`` is the configured default rather
    than a model-specific window — the status bar prefixes ``~`` in that case.
    """

    used: int
    total: int
    percent: float
    estimated: bool

    @property
    def over_warning(self) -> bool:
        """Whether usage has crossed the 80% warning threshold."""
        return self.percent >= ContextMeter.WARNING_PERCENT


class ContextMeter:
    """Compute context-window % and session cost from token counts.

    Delegates window resolution to :func:`get_model_max_input_tokens` — the one
    resolver the LLM call path itself uses — so a config change or a new LiteLLM
    catalogue entry is honored without a second implementation to keep in sync.
    Inject ``windows``/``default_window`` in tests to pin the total without a
    real config or LiteLLM catalogue lookup.
    """

    #: Usage at or above this percent is rendered in the warning style.
    WARNING_PERCENT: float = 80.0

    def __init__(
        self,
        *,
        windows: dict[str, int] | None = None,
        default_window: int | None = None,
    ) -> None:
        """Bind optional explicit window overrides (else resolve live)."""
        self._windows = windows
        self._default_window = default_window

    # -- context window ---------------------------------------------------

    def _resolve_window(self, model: str | None) -> tuple[int, bool]:
        """Return ``(total_tokens, estimated)`` for ``model``.

        With no injected override this defers entirely to
        :func:`get_model_max_input_tokens` for ``total`` — user override, then
        LiteLLM's catalogue, then the configured default. ``estimated`` is
        ``True`` only when neither of the first two legs answered.
        """
        if self._windows is None and self._default_window is None:
            total = get_model_max_input_tokens(model)
            if not model:
                return total, True
            overrides = _load_context_overrides()
            bare = _strip_provider_prefix(model)
            has_override = model in overrides or bare in overrides
            # Prefixed name first, matching get_model_max_input_tokens's own
            # lookup order — a proxy-bridge-hydrated entry lives under the
            # prefixed spelling and would otherwise read back as "estimated"
            # despite total already resolving to the real window.
            known_to_litellm = any(
                _litellm_max_input_tokens(candidate) is not None
                for candidate in dict.fromkeys((model, bare))
            )
            return total, not (has_override or known_to_litellm)

        # Injected path (tests): exact, then suffix match — config may key by
        # bare name while the runtime model carries a proxy prefix (or vice
        # versa) — same matching the live resolver applies.
        windows = self._windows or {}
        if model:
            if model in windows:
                return int(windows[model]), False
            bare = model.rsplit("/", 1)[-1]
            for key, value in windows.items():
                if key == bare or key.rsplit("/", 1)[-1] == bare:
                    return int(value), False
        default = self._default_window if self._default_window is not None else 128000
        return int(default), True

    def usage(self, *, used_tokens: int, model: str | None) -> ContextUsage:
        """Compute :class:`ContextUsage` for ``used_tokens`` against ``model``.

        ``percent`` is clamped to ``[0, 100]`` and ``0`` when the window is
        non-positive (never divides by zero, never raises).
        """
        total, estimated = self._resolve_window(model)
        used = max(0, int(used_tokens))
        if total <= 0:
            percent = 0.0
        else:
            percent = min(100.0, max(0.0, used / total * 100.0))
        return ContextUsage(used=used, total=total, percent=percent, estimated=estimated)

    # -- cost -------------------------------------------------------------

    def cost(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        model: str | None,
    ) -> float | None:
        """Return the session cost in USD, or ``None`` when price is unknown.

        Uses LiteLLM's per-token price table. A model LiteLLM does not price (or
        any lookup error) returns ``None`` — the caller renders ``—`` rather
        than a fabricated figure.
        """
        if not model:
            return None
        try:
            import litellm
        except Exception:
            return None
        # Silence LiteLLM's stderr "Provider List" banner on an unknown model —
        # an unpriced model is an expected, handled case here (we return None).
        litellm.suppress_debug_info = True
        candidates = [model, model.rsplit("/", 1)[-1]]
        for name in candidates:
            try:
                prompt_cost, completion_cost = litellm.cost_per_token(
                    model=name,
                    prompt_tokens=max(0, int(input_tokens)),
                    completion_tokens=max(0, int(output_tokens)),
                )
            except Exception:
                continue
            total = float(prompt_cost) + float(completion_cost)
            if total >= 0:
                return total
        return None

    # -- formatting helpers (pure) ----------------------------------------

    @staticmethod
    def format_tokens(count: int) -> str:
        """Format a token count compactly: ``842``, ``12.3K``, ``1.4M``."""
        count = max(0, int(count))
        if count >= 1_000_000:
            return f"{count / 1_000_000:.1f}M"
        if count >= 1_000:
            return f"{count / 1_000:.1f}K"
        return str(count)

    @staticmethod
    def format_cost(cost: float | None) -> str:
        """Format a USD cost honestly: ``—`` when unknown, else ``$0.0123``."""
        if cost is None:
            return "—"
        if cost < 0.01:
            return f"${cost:.4f}"
        return f"${cost:.2f}"


__all__ = ["ContextMeter", "ContextUsage"]
