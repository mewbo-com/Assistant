# Agentic Apps

## Apps an agent builds and runs

<div style="display: flex; justify-content: center;">
  <img src="../assets/img/mewbo-apps-01-detail.png" alt="A live Mewbo App called LLM Model Compare, version 1, marked Live. A left filter rail covers creator or provider, release year, capabilities, minimum intelligence index, minimum output tokens per second, and maximum blended cost. The center shows stat tiles and a bar chart ranked by Coding, followed by a ranked list. A right rail shows Health with last refreshed and next refresh time and the maintainer, Recent runs, Pipelines refreshing daily, the Cron schedule, and Versions." style="width: 100%; max-width: 960px; height: auto;" />
</div>

Describe an app in one sentence. A builder agent reads your real files, derives the data model, writes a Streamlit frontend and the pipelines that feed it, then puts it online. From then on the platform keeps it fresh, and the steady state costs you nothing.

---

## How it works

```mermaid
flowchart LR
    D([Describe your app]) --> B["Builder agent\ngenerates frontend, pipelines, schedule"]
    B --> P["Platform runs pipelines\non their schedule"]
    P -->|"zero-token steady state"| C[("Collections")]
    C --> A["Your Streamlit app\nlive-reads every rerun"]
    P -.->|"only if a pipeline breaks"| R([Repair])
    R -.-> P
```

The steady state is plain code on a schedule. Your app live-reads the collections those pipelines write, so it renders current data and never a frozen copy. A model run re-enters the loop only when a pipeline breaks. `LLM Model Compare` above is one of these, refreshing its model snapshots daily.

---

## What you get

- **A live Streamlit app.** Code moves and parses the data. Agentic pipelines exist only for what code can't express.
- **An honest health panel.** Freshness and a schedule per pipeline, with loud warnings. Silence never stands in for `fine`.
- **Versioned history.** Every submit is a version, and rollback is one step.

---

## An app, not a widget

A **widget** is a snapshot, frozen at creation. An **app** stays alive, because its pipelines keep running and tomorrow it shows tomorrow's data.

---

## What it costs

Building spends model effort once, a few minutes of agent time. Repairs and agentic pipelines are the only recurring spend after that, and [Living with an App](operating.md) prices every kind of refresh.

---

## Where it lives

The **Apps** tab in the Mewbo Console, a peer of Tasks, Wiki, and Search. The same apps render in the Aura Android client, and any surface advertising the `apps` capability gets the tab.

→ [Building an App](building.md): the create flow and what the builder does.

→ [Living with an App](operating.md): health, refreshes, versions, and repair.
