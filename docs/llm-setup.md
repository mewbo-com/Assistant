# LLM Setup

## Connect a model provider

Mewbo needs two keys to start, an API key and a default model, set in `configs/app.json`.

## Minimum configuration
Start from the shipped example rather than an empty file.

```bash
cp configs/app.example.json configs/app.json
```

Then set these two keys in [`configs/app.json`](repo:configs/app.example.json).

```json title="configs/app.json"
{
  "llm": {
    "api_key": "sk-ant-xxxxxxxx",
    "default_model": "anthropic/claude-sonnet-4-6"
  }
}
```

That's it. LiteLLM routes `anthropic/claude-sonnet-4-6` to the Anthropic API using the key you provide, and direct provider access needs no `api_base` URL.

> [!TIP] Edit it in the console
> The console's **Settings → Models & Inference** panel writes this same config. Its fields map one to one onto the keys documented on this page.

<div style="display: flex; justify-content: center;">
  <img src="../assets/img/mewbo-settings-01-models.jpg" alt="The Models & Inference panel of the Mewbo console Settings screen, with fields for the API base, a configured (write-only) API key, and the default, action-plan, tool, and title model IDs" style="width: 100%; max-width: 880px; height: auto;" />
</div>

## Optional LLM configuration
| Key | Purpose | Notes |
| --- | --- | --- |
| `llm.api_base` | Base URL override. | Only needed when using a proxy (LiteLLM, Bifrost). Leave empty for direct provider access. |
| `llm.action_plan_model` | Model for plan generation. | Falls back to `llm.default_model` if unset. |
| `llm.tool_model` | Model for tool execution. | Falls back to `llm.action_plan_model`, then `llm.default_model`. |
| `llm.reasoning_effort` | Default reasoning effort level. | Values: `low`, `medium`, `high`, `none`. |
| `llm.reasoning_effort_models` | Allowlist for reasoning effort. | Supports exact matches and `*` suffix wildcards. |
| `llm.proxy_model_prefix` | Prefix prepended to model names when routing through a proxy. | Default: `"openai"`. Set to match your proxy's expected provider prefix. Falls back to `"openai"` when empty. |

## Model fallback

When the primary model fails with a retryable error, Mewbo walks an ordered fallback list before giving up. That is what carries a run through a rate limit or a provider outage. It is off by default and needs an explicit opt-in in [`configs/app.json`](repo:configs/app.example.json).

```json title="configs/app.json"
{
  "llm": {
    "default_model": "anthropic/claude-sonnet-4-6",
    "fallback": {
      "enabled": true,
      "models": [
        "openai/gpt-4o",
        "anthropic/claude-haiku-4-5"
      ]
    }
  }
}
```

The flat `llm.fallback_models` array is also honoured. It supplies the ladder when `fallback.models` is empty, and a non-empty value there counts as fallback enabled. Prefer the typed `fallback` block.

Each fallback gets one attempt, and the error class decides what happens next.

- **Transient errors**, such as rate limits or timeouts, retry the primary model first, then cascade.
- **Context overflow** skips straight to the next model.
- **Auth errors** abort immediately, since retrying the same provider wouldn't help.

`agent.llm_call_retries` sets the retries against the primary before cascading, and defaults to 2.

## MCP setup
MCP servers are optional, and each one adds external tools to the registry. Create `configs/mcp.json` or run `/mcp init` in the CLI, then start a client once to discover tools and cache the manifest under `~/.mewbo/`. [MCP Tools](features-mcp.md) has the file schema and the per-project merge.

## LiteLLM provider support
The LLM layer runs on LiteLLM through `langchain-litellm`. Model IDs use `provider/model` syntax, for example `anthropic/claude-sonnet-4-6`, `openai/gpt-4o`, or `mistral/mistral-small`. LiteLLM routes each one to the correct API. Behind a proxy with no provider prefix on the model name, `llm.proxy_model_prefix` is prepended to route to an OpenAI-compatible endpoint.
