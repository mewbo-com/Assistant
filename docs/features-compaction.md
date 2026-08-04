# Compaction

## Keep a long session going

A long session hits the model's context limit and the run fails. Compaction is what stops that. Older turns are summarised into a compact record that rides along in each following prompt, so the session carries on without losing the thread of work.

It runs on its own, and nothing about how you use the session changes after it does. You can also force it, from any surface.

---

## When compaction runs

### Automatic

Automatic compaction fires when the most recent root prompt crosses `token_budget.auto_compact_threshold`, which defaults to 80 percent of the model's context window. The check runs after every LLM call against the `input_tokens` the provider reports, not a character count estimate, so the threshold holds even for models with unusual tokenisation.

The console's [context window bar](features-token-usage.md#the-context-window-bar-console) marks that threshold. Compaction runs when the fill reaches the marker.

### Manual

| Interface | Command |
|-----------|---------|
| CLI | `/compact` or `/summarize` |
| Console | Compact button in the session header toolbar |
| API | [POST /api/sessions/{session_id}/query](endpoint:POST /api/sessions/{session_id}/query) with `{"query": "/compact"}` |

---

## Two modes: PARTIAL and FULL

The two modes trade context detail against context freshness.

### PARTIAL (default for auto-compact)

Keeps the most recent events verbatim, `context.recent_event_limit` of them with a default of 8, and summarises everything older. Recent context stays intact, so the model does not lose the file it is editing or the error it is chasing. That makes it the mode for work in progress.

### FULL

Summarises the entire transcript, recent events included, and builds the next prompt from a clean slate. Use it when a task has finished, or to reset context pressure before a new phase of work in the same session.

### Forcing a mode

Pass the mode explicitly.

```
/compact full
/compact partial
```

Or use the API.

```
POST /api/sessions/{id}/query
{"query": "/compact full"}
```

---

## Caveman mode

`compaction.caveman_mode` switches to a terser summary prompt. It drops articles, filler and hedging from the prose while preserving code blocks, file paths, URLs, commands and error strings verbatim. On prose-heavy sessions it cuts compaction output tokens by roughly 30 to 60 percent.

```json title="configs/app.json"
"compaction": {
  "caveman_mode": true
}
```

---

## After the summary

The most recently touched files are read back into the compacted context, so a file the model was partway through editing arrives with its current contents already in view. [Compaction pipeline](core-orchestration.md#compaction) gives the limits on that.

Sub-agents survive it untouched. Their state lives outside the LLM conversation, so the agent tree, the progress notes and the results are all still there afterwards.

---

## Routing compaction to a different model

Compaction uses the session's own model by default. `llm.compact_models` routes it to a cheaper or faster one instead, such as a small Haiku class model for summarising.

```json title="configs/app.json"
"llm": {
  "compact_models": ["anthropic/claude-haiku-4-5-20251001", "default"]
}
```

Models are tried in priority order and the next entry takes over on failure. `"default"` resolves to the running agent's model.

---

## Seeing compactions in the UI

Each compaction is a distinct pill in the console timeline, and the context bar popover carries a Compactions row with the run count and the total tokens reclaimed over the life of the session.

---

## Configuration

| Key | Default | Description |
|-----|---------|-------------|
| `token_budget.auto_compact_threshold` | `0.8` | Fraction of the context window (0.0–1.0) at which auto-compact fires. |
| `context.recent_event_limit` | `8` | Events kept verbatim in PARTIAL mode. Everything older is summarised. |
| `llm.compact_models` | `["default"]` | Priority-ordered model list for compaction. `"default"` = agent's own model. |
| `compaction.caveman_mode` | `false` | Enable terse summarization prompt (~30–60% fewer output tokens). |

Window sizing lives on [Token Usage](features-token-usage.md#configuration), and [configuration.md](configuration.md#token-budget) has the full schema.

> [!NOTE] How it works internally
> See [Architecture Overview → Compaction pipeline](core-orchestration.md#compaction).
