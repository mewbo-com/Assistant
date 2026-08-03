"""Everything that turns engine state into a provider call and back.

Model construction, the retry/fallback ladder, per-model request shaping, and
prompt assembly — the four modules a change to "how a turn reaches the model"
has to touch, and the home for the cross-model normalization law (differences
between providers are absorbed at the LiteLLM/adapter seam, never by sniffing
text format further up).

This ``__init__`` is deliberately EMPTY of code. ``llm.py`` imports
``litellm``, whose own module body calls ``load_dotenv()`` — reading a ``.env``
off disk and mutating ``os.environ`` at import time. A convenience re-export
here would drag that side effect into ``model_variants`` and
``prompt_registry``, which do not pay it today. That is invisible in a diff,
which is why it is a rule rather than a preference.
"""
