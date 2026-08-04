# Get Started

## Run Mewbo in your terminal

<video controls preload="metadata" width="2400" height="1350">
  <source src="../assets/videos/mewbo-cli-01-video.mp4" type="video/mp4" />
  Your browser does not support the video tag.
</video>

Run Mewbo agent sessions in a full screen terminal app, against your real files and your real shell. Launch it from your shell, type a request, and watch the run stream live on one screen.

You drive an entire run from the keyboard. The engine runs on your machine, so the core loop makes no API round trip.

The [web console](../web/index.md) and the [Android app](../android/index.md) fit a graphical surface or a session on your phone. The terminal fits speed and locality at your desk.

The interface is one [Textual](https://textual.textualize.io/) application, launched from [`cli_master.py`](repo:apps/mewbo_cli/src/mewbo_cli/cli_master.py) and defined in [`app.py`](repo:apps/mewbo_cli/src/mewbo_cli/tui/app.py).

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Python 3.10+ | [uv](https://docs.astral.sh/uv/) manages the virtualenv |

The terminal client ships with the base install, so no extra is needed.

## Install and first run

1. Install the dependencies with `uv sync`.
2. Launch the app with `uv run mewbo`. It opens full screen.
3. On a fresh machine, type `/init` and press ++enter++ to scaffold the config files Mewbo needs.
4. Type a request in plain language and press ++enter++.
5. Approve or deny when the agent asks. Mewbo prompts you before any write or shell command.
6. Type `/help` to list the commands, and `/exit` when you are done.

Run `uv tool install .` from the repository root to put `mewbo` on your PATH and start a session from any directory.

## Configuration

Pass `--config` at launch to name one specific file. Otherwise Mewbo walks a config chain rooted at your project.

- [LLM Setup](../llm-setup.md) covers provider keys and model choice.
- [Configuration Reference](../configuration.md) lists every config key on the site.

## Next steps

<div class="ms-grid ms-grid--4">

<a class="ms-card" href="interface/">
<span class="ms-card__title">The Interface</span>
<span class="ms-card__body">The transcript, tool cards, the plan dock and the approval modals.</span>
</a>

<a class="ms-card" href="agent-fleet/">
<span class="ms-card__title">Agent Fleet</span>
<span class="ms-card__body">The sidebar tracking every sub-agent, and the drill in view behind each row.</span>
</a>

<a class="ms-card" href="remote-sync/">
<span class="ms-card__title">Remote Sync</span>
<span class="ms-card__body">Local by default, and what reaches the network when you opt in.</span>
</a>

<a class="ms-card" href="configuration/">
<span class="ms-card__title">Configuration</span>
<span class="ms-card__body">Launch flags, the config chain and session recovery.</span>
</a>

</div>
