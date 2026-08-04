# Agentic Wiki

## Generate docs from your code

<video controls preload="metadata" width="1920" height="1080">
  <source src="../assets/videos/mewbo-wiki-qna-demo.mp4" type="video/mp4" />
  Your browser does not support the video tag.
</video>

Paste a repository URL and Mewbo writes the documentation for it. The **Agentic Wiki** lifts the
code into a graph and generates a wiki from it, every page citing the source files it was written
from. Ask a question on any page and probe agents answer from that same graph.

Inside the product, the Q&A box and the README badge still carry the older **MewboWiki** name.

---

## What you get

A generated wiki is a full documentation site for one repository.

- **An overview that reads like docs, not a file dump.** Prose pages cover the runtime model, the
  architecture and the conventions, with flow diagrams. Each lists the *Relevant source files* behind
  it.
- **A graph that makes answers traceable.** Structural symbols, abstract entities and anchored notes
  are three layers of one graph, and there is a 3D view over
  it. → [The Knowledge Graph](features-wiki-graph.md)
- **Ask the Agentic Wiki.** Every page carries an inline Q&A box. Pick a model and probe agents read
  the codebase in parallel before one cited answer is
  written. → [Question Answering](features-wiki-qa.md)
- **Copy badge.** A markdown snippet for your README, linking back to the wiki.

<div class="swiper ms-shots">
<div class="swiper-wrapper">
<div class="swiper-slide"><figure><img loading="lazy" src="../assets/img/mewbo-wiki-01-landing.jpg" alt="The Agentic Wiki landing page in the Mewbo Console: a repository URL field with a Generate Wiki button, a resumable 'Incomplete indexes' notice, and cards for already-indexed projects (acme/beacon, bearlike/Assistant, bearlike/Grove) showing their host, branch, and page counts" /><figcaption>Start from a repository URL, or reopen a project you already indexed</figcaption></figure></div>
<div class="swiper-slide"><figure><img loading="lazy" src="../assets/img/mewbo-wiki-02-overview.jpg" alt="An Agentic Wiki overview page for Grove showing the Grove Overview article with a runtime-model flow diagram, a left-hand page index, an On this page outline, a Re-index control, and an Ask MewboWiki question box" /><figcaption>Prose pages with flow diagrams, grounded in the source files they cite</figcaption></figure></div>
<div class="swiper-slide"><figure><img loading="lazy" src="../assets/img/mewbo-wiki-08-qna.jpg" alt="An Agentic Wiki answer to 'What is this project for?': a summary card, an expandable Cited Sources list, a Retrieval details panel listing the pages accessed and the model used, and a cited answer with inline wiki citation chips and a follow-up question box" /><figcaption>Cited answers with the exact sources that backed them</figcaption></figure></div>
<div class="swiper-slide"><figure><img loading="lazy" src="../assets/img/mewbo-wiki-07-badge.jpg" alt="The 'Add the wiki badge' popover in the Agentic Wiki, previewing an 'Ask Mewbo Wiki' badge and the markdown snippet to drop into a repository README, beside the Edit Wiki, Graph, Copy badge, and Copy link toolbar" /><figcaption>Copy a README badge that links straight back to the wiki</figcaption></figure></div>
</div>
<div class="swiper-pagination"></div>
<div class="swiper-button-prev"></div>
<div class="swiper-button-next"></div>
</div>

---

## Explore the docs { .ms-h2-icon data-icon="book" }

The Agentic Wiki has three moving parts.

<div class="ms-grid ms-grid--3">

<a class="ms-card" href="../features-wiki-indexing/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:database" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Indexing &amp; Generation</span>
  <span class="ms-card__body">Point at a repo and a run of seven phases writes the pages. A short wizard scopes the run first. Re-indexing later touches only what changed.</span>
</a>

<a class="ms-card" href="../features-wiki-qa/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:message-circle-question" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Question Answering</span>
  <span class="ms-card__body">A coordinating agent fans out probe agents across the graph, then synthesises one cited answer. Your agent fleet can ask the same question over MCP.</span>
</a>

<a class="ms-card" href="../features-wiki-graph/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:waypoints" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">The Knowledge Graph</span>
  <span class="ms-card__body">The multiplex graph under the pages. It is what carries an answer across real relationships in the code rather than a handful of similar snippets.</span>
</a>

</div>

---

## Enabling it

The wiki ships as an optional extra on the API server.

```bash
# Install with the wiki extras
uv sync --extra wiki

# Run the API server
uv run mewbo-api
```

The `/v1/wiki/*` routes mount only when the extras resolve. Without them the server still starts and
the feature is absent. Storing wikis and streaming progress both need the MongoDB backend.

> [!NOTE] Tokens for a private repo are remembered
> Access tokens entered during the indexing wizard are persisted, so re-index runs don't prompt you
> again. If a token is revoked or expires, the next re-index detects the failure and prompts for a
> replacement.

> [!NOTE] Going deeper
> [Sub-agents](features-agents.md) covers the delegation model that writes pages in parallel, and
> [Architecture Overview](core-orchestration.md) covers the session runtime underneath.
