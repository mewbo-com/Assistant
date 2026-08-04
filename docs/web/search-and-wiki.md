# Search and Wiki

## Get cited answers about a repo

The console hosts two products built on Mewbo's engine. Agentic Search runs a question across many sources and returns a cited answer. The Wiki turns a repository into browsable pages, a question and answer surface, and a 3D code graph. This page is the console surface for both.

## Agentic Search

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-search-01-landing.jpg" alt="The Agentic Search landing page with the search composer and saved workspaces" style="width: 100%; max-width: 880px; height: auto;" />
</div>

Search lives at the `/search` route, driven by [`AgenticSearchView`](repo:apps/mewbo_console/src/components/agentic_search/AgenticSearchView.tsx). A question fans out across the sources saved in a workspace and comes back as a synthesized answer with its supporting results.

### Run a search

Type a question into the search composer and submit. The landing page is inert until you do, and opening it never starts a run on its own.

The composer toolbar carries exactly two controls. One names the active workspace. The other is [`SearchScopeControl`](repo:apps/mewbo_console/src/components/agentic_search/SearchScopeControl.tsx), holding the tier, the model and the source configuration behind one menu. Its pill names the resolved setting at rest, so the current tier and model read without opening anything.

### Tiers

The tier is the run's one budget knob. [Agentic Search](../features-search.md#search-tiers) gives the three settings and what each spends. Picking one from the scope menu clears any model override, so a stale model from a previous tier never silently wins. You can still override the model for a single run from the same menu, without editing your config.

### The results and cited answer view

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-search-02-results.jpg" alt="The Agentic Search results view with a synthesized answer, result cards, and the trace rail" style="width: 100%; max-width: 880px; height: auto;" />
</div>

Everything in the results view derives from events the run actually emitted, never from a synthetic timer.

**The answer.** The synthesized answer streams in at the top as markdown. Its confidence and source count show only when the run earned them. An unknown value is left blank, never faked as zero.

**Result cards.** Each card is a reading surface for one supporting result, expanding only when there is more to show. The title links out when a URL exists, and a button prefills the composer with another question about that result.

**The instrument rail.** The right rail reads instruments for each lane and for the run as a whole, leading with its kind and a pip counting its results. A field renders only when present. Below 1100 pixels the rail hides and the trace stays reachable from the run meta row.

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-search-04-agent-trace.jpg" alt="The Agentic Search agent trace drawer showing per-lane instrument detail" style="width: 100%; max-width: 880px; height: auto;" />
</div>

**The agent trace.** Open the trace for each lane's kind, model, steps and terminal evidence. From the run meta row you can also open the underlying agent session, cancel a running search, or go one tier deeper. A finished run is [shareable as a link](../features-search.md#share-a-run-with-a-link).

For the engine itself and the graph of source capabilities behind it, see [Agentic Search](../features-search.md) and [Source Capability Graph](../features-search-scg.md).

## Wiki

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-wiki-01-landing.jpg" alt="The Agentic Wiki landing page showing a gallery of indexed repositories" style="width: 100%; max-width: 880px; height: auto;" />
</div>

The Wiki lives at `/wiki`, resolved by [`WikiApp`](repo:apps/mewbo_console/src/components/wiki/WikiApp.tsx). To index a repository, browse the gallery and follow the configure wizard. See [Wiki indexing](../features-wiki-indexing.md) for that flow.

Registering a repository costs nothing and starts nothing. Generating the wiki is the deliberate step.

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-wiki-04-index-source.jpg" alt="The configure-indexing wizard's first step, choosing a Git platform and repository URL to index" style="width: 100%; max-width: 880px; height: auto;" />
</div>

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-wiki-09-indexing-progress.png" alt="A live indexing progress card showing the clone, scan, graph, enrich, plan, pages, and finalize stages" style="width: 100%; max-width: 720px; height: auto;" />
</div>

### Browse the pages

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-wiki-02-overview.jpg" alt="An Agentic Wiki page with the navigation sidebar, article content, and table of contents" style="width: 100%; max-width: 880px; height: auto;" />
</div>

The article renders as markdown with syntax highlighting and diagrams, between a page navigator and a table of contents that tracks your position. A maintainer can copy a badge for any page, linking a README straight back into the wiki.

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-wiki-07-badge.jpg" alt="The Add the wiki badge dialog, with a markdown snippet for embedding an Ask Mewbo Wiki badge in a README" style="width: 100%; max-width: 640px; height: auto;" />
</div>

### Ask a question with citations

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-wiki-08-qna.jpg" alt="The Agentic Wiki question-and-answer view with an answer, inline citation chips, and source cards" style="width: 100%; max-width: 880px; height: auto;" />
</div>

Ask from the floating composer and the answer streams into the left column of [`QAScreen`](repo:apps/mewbo_console/src/components/wiki/QAScreen.tsx), with [citation chips](../features-wiki-qa.md#grounded-cited-answers) inline in the prose. Click a chip and a file with a known repository opens at the exact line range in a new tab. Otherwise the chip scrolls to a source card holding the cited evidence.

A completed answer is addressable through a `?answer=<id>` URL, so a refresh or a shared link replays it with no new model call.

For the retrieval and citation model behind the Wiki, see [Wiki](../features-wiki.md) and [Wiki question and answer](../features-wiki-qa.md).

### The 3D code graph

The Wiki includes an interactive 3D graph of the repository. It is a WebGL Code Galaxy, not a flat diagram, rendered through the shared engine [`Graph3DView`](repo:apps/mewbo_console/src/components/wiki/Graph3DView.tsx) that also drives [`KnowledgeGraph3DScreen`](repo:apps/mewbo_console/src/components/wiki/KnowledgeGraph3DScreen.tsx).

It draws all [three layers of the knowledge graph](../features-wiki-graph.md#three-layers-one-graph). Folders start collapsed and expand on demand, keeping a large repository responsive. The toolbar filters nodes by name, toggles kinds and layers, and fits or resets the view. Click a node to inspect it in a side panel. The same engine powers the Agentic Search workspace graph, where the nodes are mapped source capabilities and their anchored memory notes.

For the graph substrate and its schema, see [Wiki code graph](../features-wiki-graph.md).

## Next steps

- [Sessions](sessions.md). The session list, the composer and live streaming.
- [Agentic Search](../features-search.md). The engine, its tiers and retrieval across sources.
- [Source Capability Graph](../features-search-scg.md). The graph that backs search over saved sources.
- [Wiki](../features-wiki.md). Indexing, page generation and grounded answers.
- [Wiki code graph](../features-wiki-graph.md). The multiplex graph substrate.
