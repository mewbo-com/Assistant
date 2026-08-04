# Token Usage & Caching

## See what a session costs

Every session's token consumption is tracked and reported, split between the root agent and any sub-agents it spawns, in the web console, the CLI and the REST API. [Prompt caching](#prompt-caching) cuts what repeat turns cost.

---

## The context window bar (console)

The bar sits in the console navbar and in each session's detail header, showing how full the root agent's context window is right now.

```
ctx ████░░░░░░░░ 42k/200k
         ▲ reserved-for-compact
```

| Segment | Color | Meaning |
|---------|-------|---------|
| Used fill | Foreground / Primary / Destructive (escalates) | Tokens in the most recent root prompt |
| Reserved | Accent | Buffer reserved for the auto-compact threshold |
| Available | Background | Remaining usable space |

| Remaining | Fill color |
|-----------|-----------|
| > 20% | Foreground (neutral) |
| 10%–20% | Primary (warning) |
| < 10% | Destructive (critical) |

Click the bar for a popover carrying the full breakdown. Current and peak context pressure, billed totals, cache reads and writes, reasoning tokens, and the running compaction count.

---

## Per-turn token chip

Each turn in the session timeline carries a token chip with the subtotals for the most recent root LLM call.

| Field | Description |
|-------|-------------|
| Input tokens | Prompt size billed by the provider |
| Output tokens | Response tokens billed by the provider |
| Cache read tokens | Served from the provider's prompt cache, at a discount |
| Cache write tokens | Written to the provider's prompt cache this turn |
| Reasoning tokens | Hidden thinking tokens from extended-thinking models |

Both cache fields read zero on a model without prompt caching, and reasoning tokens read zero without extended thinking.

---

## Root vs sub-agent split

The split tells you whether token pressure comes from the orchestrator or from the workers under it. A session that spawns many sub-agents typically shows low root pressure alongside a high combined sub-agent total. The console footer and the context bar popover show both counts side by side, and the [usage API](#usage-api) breaks them out as separate fields.

---

## Usage API

```
GET /api/sessions/{session_id}/usage
X-Api-Key: <your-token>
```

All token fields are integers, and read zero for sessions or events that predate cache tracking.

> [!NOTE] Counts are only as good as what the provider reports
> The rollup comes from the `llm_call_end` events in the transcript, so a field the provider never reported reads `0`. The example below is illustrative rather than measured. Langfuse carries the same per-call figures if you need a second source.

```json
{
  "root_model": "anthropic/claude-sonnet-4-6",
  "root_max_input_tokens": 200000,
  "root_last_input_tokens": 42150,
  "root_utilization": 0.2108,
  "tokens_until_compact": 117850,
  "compact_threshold": 0.8,

  "root_peak_input_tokens": 55000,
  "sub_peak_input_tokens": 31000,

  "root_input_tokens_billed": 310000,
  "sub_input_tokens_billed": 95000,
  "total_input_tokens_billed": 405000,

  "root_output_tokens": 18200,
  "sub_output_tokens": 9400,
  "total_output_tokens": 27600,

  "root_cache_creation_tokens": 12000,
  "root_cache_read_tokens": 180000,
  "root_reasoning_tokens": 0,
  "sub_cache_creation_tokens": 3000,
  "sub_cache_read_tokens": 40000,
  "sub_reasoning_tokens": 0,
  "total_cache_creation_tokens": 15000,
  "total_cache_read_tokens": 220000,
  "total_reasoning_tokens": 0,

  "root_llm_calls": 8,
  "sub_llm_calls": 6,
  "sub_agent_count": 2,

  "compaction_count": 1,
  "compaction_tokens_saved": 28000
}
```

The names that do not read literally.

| Field | Description |
|-------|-------------|
| `root_last_input_tokens` | Most recent root prompt size. Drives the context bar fill. |
| `root_utilization` | `root_last_input_tokens / root_max_input_tokens` |
| `compact_threshold` | The configured `token_budget.auto_compact_threshold` fraction |
| `sub_peak_input_tokens` | Sum of per-sub-agent peak inputs, not one worst case |
| `*_input_tokens_billed` | Cumulative billable input across all calls, cached portions included |
| `*_reasoning_tokens` | Hidden thinking tokens, billed as output |

---

## CLI usage display

`/tokens` and `/budget` are aliases. Both print the current session's budget table.

```
Token Budget
┌──────────────────────────┬──────────┐
│ Metric                   │ Value    │
├──────────────────────────┼──────────┤
│ Summary tokens           │ 1 842    │
│ Event tokens             │ 40 308   │
│ Total tokens             │ 42 150   │
│ Context window           │ 200 000  │
│ Remaining                │ 157 850  │
│ Utilization              │ 21.1%    │
│ Auto-compact threshold   │ 80.0%    │
└──────────────────────────┴──────────┘
```

A session that has not yet made a call falls back to a local estimate.

---

## Prompt caching

Caching is on already if your model supports it. Nothing to configure.

### Supported providers

| Provider | Cache mechanism | Discount |
|----------|----------------|----------|
| Anthropic | Content-block `cache_control` markers | Cache reads billed at 0.1× input |
| OpenAI | Automatic prefix caching | Cache reads billed at 0.5× input |
| AWS Bedrock | TTL-based caching | Provider-specific |

One usage API covers all three, so the syntax differences between them never reach you.

### Proxy models

A proxy set through `llm.api_base` has to advertise model capabilities for caching to activate. One that does not report caching support leaves it disabled for that model rather than risking malformed requests. [Token tracking](core-orchestration.md#token-tracking) covers what the proxy must expose.

### Seeing cache savings

Per-turn savings land in the token chip as cache read tokens, and the running session total in the context bar popover under Cache reads. Programs read `total_cache_read_tokens` from [GET /api/sessions/{session_id}/usage](endpoint:GET /api/sessions/{session_id}/usage).

---

## Configuration

These two decide the denominator every percentage on this page is measured against.

| Key | Default | Description |
|-----|---------|-------------|
| `token_budget.default_context_window` | `128000` | Fallback window size when LiteLLM doesn't know the model. |
| `token_budget.model_context_windows` | `{}` | Per-model overrides (map of model name → token count). Use to cap below the real max or for proxy-only models. |

[Compaction](features-compaction.md#configuration) carries the threshold and summarisation keys, and [configuration.md](configuration.md#token-budget) has the full schema.

> [!NOTE] How it works internally
> See [Architecture Overview → Token tracking](core-orchestration.md#token-tracking).
