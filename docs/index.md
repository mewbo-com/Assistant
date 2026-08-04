<!--
  Maintainer note: this landing page is a decision surface, not a sitemap.
  The sidebar already enumerates pages. Every section here should help a
  visitor self-select a path (surface, journey stage, capability cluster).
  Keep positioning aligned with README.md when core messaging changes.
-->

<section class="ms-hero" markdown>

<p class="ms-hero__eyebrow">Documentation</p>

### Introducing Mewbo

<p class="ms-hero__lede">
Mewbo is an open stack for handing agents to your engineers and operators, inside the
permissions you set. It plugs into the tools and systems you already run.
Automation, wiki, search and apps stand on one harness, whichever model you bring.
</p>

<div class="ms-cta-row">
  <a class="ms-btn ms-btn--primary" href="getting-started/">Quickstart →</a>
  <a class="ms-btn ms-btn--secondary" href="deployment-docker/">Install via Docker</a>
  <a class="ms-btn ms-btn--ghost" href="api/">API reference</a>
</div>

<div class="ms-pills" aria-label="What makes Mewbo different">
  <span class="ms-pill">
    <iconify-icon class="ms-pill__icon" icon="lucide:git-fork" width="14" height="14" aria-hidden="true"></iconify-icon>
    Parallel sub-agents
  </span>
  <span class="ms-pill">
    <iconify-icon class="ms-pill__icon" icon="lucide:check-check" width="14" height="14" aria-hidden="true"></iconify-icon>
    Claude Code and Codex compatible
  </span>
  <span class="ms-pill">
    <iconify-icon class="ms-pill__icon" icon="lucide:server" width="14" height="14" aria-hidden="true"></iconify-icon>
    Self hosted, any model
  </span>
  <span class="ms-pill">
    <iconify-icon class="ms-pill__icon" icon="lucide:shield-check" width="14" height="14" aria-hidden="true"></iconify-icon>
    Permissions, plan mode, sandboxed shell
  </span>
  <span class="ms-pill">
    <iconify-icon class="ms-pill__icon" icon="lucide:quote" width="14" height="14" aria-hidden="true"></iconify-icon>
    Answers cited to a path and line
  </span>
</div>

<div class="swiper ms-shots">
<div class="swiper-wrapper">
<div class="swiper-slide"><figure><img loading="lazy" src="assets/img/mewbo-console-01-front.png" alt="The Mewbo Console home listing recent sessions" /><figcaption>Your sessions at a glance</figcaption></figure></div>
<div class="swiper-slide"><figure><img loading="lazy" src="assets/img/mewbo-console-02-tasks.png" alt="A Mewbo task in the console, broken into steps with tool calls and results" /><figcaption>Inside a task, step by step</figcaption></figure></div>
<div class="swiper-slide"><figure><img loading="lazy" src="assets/img/mewbo-console-07-widgets.png" alt="Interactive widgets rendered inline in a Mewbo conversation" /><figcaption>Interactive widgets, inline in chat</figcaption></figure></div>
<div class="swiper-slide"><figure><img loading="lazy" src="assets/img/mewbo-wiki-02-overview.jpg" alt="An Agentic Wiki overview page with a runtime flow diagram and an Ask MewboWiki box" /><figcaption>Agentic Wiki: documentation grounded in your code</figcaption></figure></div>
<div class="swiper-slide"><figure><img loading="lazy" src="assets/img/mewbo-wiki-08-qna.jpg" alt="An Agentic Wiki answer to a question about a project, with a summary card, an expandable Cited Sources list, a Retrieval details panel naming the pages accessed and the model used, and the answer itself carrying inline citation chips" /><figcaption>Ask the wiki, and every claim carries its source</figcaption></figure></div>
<div class="swiper-slide"><figure><img loading="lazy" src="assets/img/mewbo-search-01-landing.jpg" alt="The Agentic Search landing page with workspaces scoped to connected sources" /><figcaption>Agentic Search: workspaces over your connected tools</figcaption></figure></div>
<div class="swiper-slide"><figure><img loading="lazy" src="assets/img/mewbo-search-02-results.jpg" alt="Agentic Search results: one ranked list across connected sources with a synthesised overview" /><figcaption>One ranked list across every tool, topped by a synthesis</figcaption></figure></div>
<div class="swiper-slide"><figure><img loading="lazy" src="assets/img/mewbo-console-05-plugins.png" alt="The Mewbo plugins page with installed plugins and marketplace listings" /><figcaption>Plugins and a marketplace to extend any session</figcaption></figure></div>
<div class="swiper-slide"><figure><img loading="lazy" src="assets/img/mewbo-apps-01-detail.png" alt="A live Mewbo App called LLM Model Compare, with a filter rail, a bar chart ranked by coding score, and a right rail showing health, recent runs, daily pipelines and versions" /><figcaption>Agentic Apps: a sub-agent writes it, then it runs on a schedule</figcaption></figure></div>
<div class="swiper-slide"><figure><img loading="lazy" src="assets/img/mewbo-triggers-01-plugins.png" alt="Two Mewbo settings panes side by side, one listing installed plugins and what each contributes, the other listing reverse invocation triggers with their cron expressions, fire counts and next fire time" /><figcaption>Reverse invocation: a trigger starts a session with nobody watching</figcaption></figure></div>
</div>
<div class="swiper-pagination"></div>
<div class="swiper-button-prev"></div>
<div class="swiper-button-next"></div>
</div>

</section>

---

## Choose your surface { .ms-h2-icon data-icon="target" }

A coding agent serves one person. Mewbo deploys once and serves every role, on the surface each person already works in.

<div class="ms-grid ms-grid--5">

<a class="ms-card" href="terminal/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:terminal" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Terminal</span>
  <span class="ms-card__body">Hand off the refactor, the migration or the flaky test you have been putting off, and stay in the shell while it works. It runs on your machine, against the branch you are already on.</span>
</a>

<a class="ms-card" href="web/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:app-window" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Web console</span>
  <span class="ms-card__body">The whole team works here. Each role sees only its granted tools, escalation is one request away, and a supervised run becomes a repeating trigger.</span>
</a>

<a class="ms-card" href="android/">
  <span class="ms-card__icon">
    <iconify-icon icon="simple-icons:android" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Android</span>
  <span class="ms-card__body">Make Mewbo the assistant your phone answers to. Ask out loud from any screen or answer a permission prompt on the move, against the same deployment your team already runs.</span>
</a>


<a class="ms-card" href="clients-nextcloud-talk/">
  <span class="ms-card__icon">
    <iconify-icon icon="simple-icons:slack" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Team chat</span>
  <span class="ms-card__body">Mention Mewbo in a channel and it works in the open. The thread is the record, so the next person picks it up without asking anyone what happened.</span>
</a>


<a class="ms-card" href="clients-email/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:mail" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Email</span>
  <span class="ms-card__body">Send it work the way you would send a colleague work. Reply in the thread to push it further, from a phone, a laptop or an inbox rule.</span>
</a>

<a class="ms-card" href="clients-home-assistant/">
  <span class="ms-card__icon">
    <iconify-icon icon="simple-icons:homeassistant" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Home Assistant</span>
  <span class="ms-card__body">Ask which rooms are free, why a machine stopped, or what tripped overnight. Every entity your site exposes is now something you can ask.</span>
</a>

</div>

---

## How it works { .ms-h2-icon data-icon="flow" }

Every product on this page runs the same three steps underneath.

### You describe an outcome

Ask in plain English on whichever surface is closest. Calls run as the model generates them, and anything the permission policy does not match comes back to you as a prompt. Switch to plan mode for unfamiliar or destructive work and the run stops at a drafted plan instead. Reject it with feedback and a revised plan comes back.

### Mewbo delegates in parallel

The root agent spawns a sub-agent for every piece that can run at once. A test run, a search, a refactor and an MCP call all execute in parallel. A stalled child is caught by the hypervisor, which injects a correction between tool steps. A budget that runs out buys a turn to wrap up rather than a bare halt.

### You get a synthesised answer, not a pile of logs

Each sub-agent returns a structured result with its status, summary, warnings, files touched and checks against the acceptance criteria. The root synthesises those into one answer, backed by a transcript of every tool call, permission prompt and compaction. Fork any message to replay that branch on a different model.

!!! note "Go deeper"

    [Architecture Overview](core-orchestration.md) covers the tool use loop, the hypervisor and the concurrency lifecycle.

---

## Highlights { .ms-h2-icon data-icon="star" }

<div class="ms-grid ms-grid--3">

<a class="ms-card" href="features-wiki/">
<span class="ms-card__title">Agentic Wiki</span>
<span class="ms-card__body">Every repository gets documentation that stays current, and a place to ask questions about it. A new teammate asks how something works instead of who wrote it, and the answer comes grounded in the code and cited so it can be checked.</span>
</a>

<a class="ms-card" href="features-search/">
<span class="ms-card__title">Agentic Search</span>
<span class="ms-card__body">One search across every system your team has connected. Ask where a decision landed and sub-agents query your trackers, chats and repositories at once, merging what they find into a single ranked answer that names its sources.</span>
</a>

<a class="ms-card" href="authentication/">
<span class="ms-card__title">Control over what actually runs</span>
<span class="ms-card__body">Giving a team an agent is a permissions decision. Your identity provider says who people are, roles and teams say what each may run, a policy checks every tool call, and the shell sits in a kernel sandbox. All four are set in one place.</span>
</a>

<a class="ms-card" href="apps/">
<span class="ms-card__title">Agentic Apps</span>
<span class="ms-card__body">Describe an app in a sentence and an agent builds it over your live systems, from the data model to the interface to the pipelines that keep it fresh. It runs in your deployment under your permission rules and repairs itself when a feed breaks.</span>
</a>

<a class="ms-card" href="clients-mcp/">
<span class="ms-card__title">Mewbo as an MCP server</span>
<span class="ms-card__body">Claude Code, Codex or Cursor calls Mewbo as a tool. Send a whole task to your deployment without leaving the editor, or ask the wiki about a repo you have never opened, while your session keeps its context on the task at hand.</span>
</a>

<a class="ms-card" href="api/structured-outputs/">
<span class="ms-card__title">Structured Outputs</span>
<span class="ms-card__body">Give your own software an agent without building one. Send a question and the JSON Schema you need back, and a managed session researches your code and tools, then returns an answer that fits it exactly.</span>
</a>

</div>

---

## Already using Claude Code or Codex? { .ms-h2-icon data-icon="plug" }

Point Mewbo at a project and it loads the MCP servers, skills, plugins and instruction files you already have.

<div class="ms-grid ms-grid--5">

<div class="ms-card">
<span class="ms-card__title">MCP servers</span>
<span class="ms-card__body">Both the Mewbo <code>servers</code> and the Claude Code / VS Code <code>mcpServers</code> schemas are accepted at project and user scope.</span>
</div>

<div class="ms-card">
<span class="ms-card__title">Skills</span>
<span class="ms-card__body"><code>SKILL.md</code> files in <code>~/.claude/skills/</code> or <code>.claude/skills/</code> activate exactly as authored, with the Agent Skills standard.</span>
</div>

<div class="ms-card">
<span class="ms-card__title">Plugins &amp; marketplaces</span>
<span class="ms-card__body">Claude Code plugin manifests install from any marketplace that already serves them, with commands, agents and tools carried over.</span>
</div>

<div class="ms-card">
<span class="ms-card__title">Project instructions</span>
<span class="ms-card__body"><code>CLAUDE.md</code>, <code>AGENTS.md</code>, and <code>.claude/rules/*.md</code> all load hierarchically on session start.</span>
</div>

<a class="ms-card" href="features-plugins/#session-tools">
<span class="ms-card__title">Session tools</span>
<span class="ms-card__body">A <code>session_tools</code> array in <code>plugin.json</code> contributes stateful tools that live for one agent.</span>
</a>

</div>

!!! note "See also"

    [Project Setup](project-configuration.md) and [Plugins &amp; Marketplace](features-plugins.md) walk through the complete compatibility matrix.

---

## What you can do { .ms-h2-icon data-icon="grid" }

<div class="ms-grid ms-grid--5">

<div class="ms-card">
<span class="ms-card__title">Workspace &amp; execution</span>
<ul class="ms-card__list">
  <li><a href="features-builtin-tools/">Built-in tools</a>: read, edit, shell, list</li>
  <li><a href="web/widgets/">Widgets</a>: interactive UI inline in chat</li>
  <li><a href="features-mcp/">External MCP tools</a>: servers Mewbo calls</li>
  <li><a href="web/ide/">Web IDE</a>: per-session code-server</li>
  <li><a href="features-lsp/">Code intelligence (LSP)</a></li>
</ul>
</div>

<div class="ms-card">
<span class="ms-card__title">Knowledge &amp; discovery</span>
<ul class="ms-card__list">
  <li><a href="features-wiki/">Agentic Wiki</a>: source-grounded repo docs</li>
  <li><a href="features-wiki-graph/">Knowledge graph</a> of the codebase</li>
  <li><a href="features-search/">Agentic Search</a> across connected MCPs</li>
  <li><a href="apps/">Agentic Apps</a>: a data layer and interface over any of it</li>
</ul>
</div>

<div class="ms-card">
<span class="ms-card__title">Composition &amp; delegation</span>
<ul class="ms-card__list">
  <li><a href="features-plugins/">Plugins and marketplace</a></li>
  <li><a href="features-agents/">Sub-agents and the hypervisor</a></li>
  <li><a href="features-skills/">Skills (Agent Skills standard)</a></li>
  <li><a href="features-compaction/">Compaction</a>: long runs that keep going</li>
</ul>
</div>

<div class="ms-card">
<span class="ms-card__title">Control &amp; safety</span>
<ul class="ms-card__list">
  <li><a href="authentication/">Identity, roles and teams</a></li>
  <li><a href="features-plan-mode/">Plan mode</a>: review before execution</li>
  <li><a href="features-permissions-hooks/">Permissions and hooks</a></li>
  <li><a href="features-sandbox/">Sandboxed execution</a>: a kernel confined shell</li>
  <li><a href="features-policies/">Policies</a>: semantic gate-checks on tool calls</li>
  <li><a href="features-monitors/">Monitors</a>: session-wide behavioural guardrails</li>
</ul>
</div>

<div class="ms-card">
<span class="ms-card__title">Under the hood</span>
<ul class="ms-card__list">
  <li><a href="core-orchestration/">Architecture overview</a></li>
  <li><a href="session-runtime/">Session runtime</a></li>
  <li><a href="features-token-usage/">Token usage and budgets</a></li>
  <li><a href="clients-mcp/">Mewbo as an MCP server</a>: agents that call in</li>
  <li><a href="rest-api/">Full API reference</a></li>
</ul>
</div>

</div>

---

## From install to production { .ms-h2-icon data-icon="route" }

<div class="ms-lifecycle">

<div class="ms-step">
<p class="ms-step__title">Install</p>
<ul class="ms-step__links">
  <li><a href="getting-started/">Get Started</a></li>
  <li><a href="deployment-docker/">Docker Compose</a></li>
</ul>
</div>

<div class="ms-step">
<p class="ms-step__title">Configure</p>
<ul class="ms-step__links">
  <li><a href="llm-setup/">LLM setup</a></li>
  <li><a href="configuration/">Configuration reference</a></li>
  <li><a href="project-configuration/">Project setup</a></li>
</ul>
</div>

<div class="ms-step">
<p class="ms-step__title">Use</p>
<ul class="ms-step__links">
  <li><a href="terminal/">Terminal</a></li>
  <li><a href="web/">Web console</a></li>
  <li><a href="android/">Android</a></li>
  <li><a href="clients-mcp/">MCP server</a></li>
</ul>
</div>

<div class="ms-step">
<p class="ms-step__title">Deploy</p>
<ul class="ms-step__links">
  <li><a href="deployment-production/">Production setup</a></li>
  <li><a href="deployment-storage/">Storage backends</a></li>
  <li><a href="features-permissions-hooks/">Permissions and hooks</a></li>
</ul>
</div>

<div class="ms-step">
<p class="ms-step__title">Extend</p>
<ul class="ms-step__links">
  <li><a href="features-mcp/">MCP tools</a></li>
  <li><a href="features-plugins/">Plugins</a></li>
  <li><a href="api/structured-outputs/">Structured Outputs</a></li>
  <li><a href="rest-api/">REST API reference</a></li>
</ul>
</div>

</div>

---

## Keep learning { .ms-h2-icon data-icon="book" }

<div class="ms-grid ms-grid--4">

<a class="ms-card" href="https://github.com/bearlike/Assistant">
  <span class="ms-card__icon">
    <iconify-icon icon="simple-icons:github" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">GitHub repo</span>
  <span class="ms-card__body">Source, issues, and releases.</span>
</a>

<a class="ms-card" href="core-orchestration/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:layers" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Architecture deep-dive</span>
  <span class="ms-card__body">The tool use loop, the hypervisor, and the lifecycle.</span>
</a>

<a class="ms-card" href="troubleshooting/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:life-buoy" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Troubleshooting</span>
  <span class="ms-card__body">Common errors and how to diagnose them.</span>
</a>

<a class="ms-card" href="https://github.com/bearlike/Assistant/releases">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:list" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Changelog</span>
  <span class="ms-card__body">Release notes and upgrade guides.</span>
</a>

</div>
