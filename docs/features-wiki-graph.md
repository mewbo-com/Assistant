# The Knowledge Graph

## Trace an answer to the code

The graph is what makes an answer traceable. Every claim the Agentic Wiki writes lands on a symbol,
a file and a note in one indexed structure, so a reader follows an answer back to the code and a
stale claim can be found and retired.

Every indexed repository also ships a **Graph** view over that structure, with a folder collapse so a
large repository stays readable. From any symbol you jump to the page that documents it. Agentic
Search renders its own capability graph in the same view.

<video controls preload="metadata" width="1920" height="1080">
  <source src="../assets/videos/mewbo-wiki-graph-demo.mp4" type="video/mp4" />
  Your browser does not support the video tag.
</video>

The view is an illustration. The graph under it carries
[question answering](features-wiki-qa.md) from a single file lookup to repository scale.

---

## Three layers, one graph

The graph is a **multiplex of three layers**, and the console has a toggle per layer.

- **Structural (AST) layer.** Files, classes, functions, methods and interfaces are the nodes. Call,
  import and definition relationships are the edges.
- **Entity layer.** The subsystems, components, and concepts that matter in the codebase, extracted
  after the AST phase. Each is grounded in real AST symbols and source prose, never in generated
  text. *Anchor edges* connect an entity to the symbols it describes.
- **Memory layer.** Atomic notes anchored to both structural symbols and entity nodes.

---

## Grounded by a code memory graph

Most documentation tools chop a repository into text and search it like prose. Mewbo builds a graph
instead. The three layers are separate dimensions over one store, which is what a **multiplex code
memory graph** means.

A question does not pull a handful of snippets that merely look similar. Keyword and semantic search
seed the entry points, then the graph expands them across **multiple hops**, from a symbol to its
callers to the module that owns them to the note explaining why.

Because every note is anchored, a stale answer cannot hide. When the code under a note changes, the
next refresh checks that note and retires it if it no longer holds.

> [!NOTE] Why multiple hops matter
> A flat search answers a question that spans modules with three disconnected results. Traversal
> follows real relationships instead. Semantic embeddings only accelerate this. Without them keyword
> retrieval still seeds the graph and the wiki keeps working.

---

## The memory grows as the wiki is used

The memory layer is not frozen at indexing time.

- **The indexer seeds it** while writing pages, with anchored facts about each subsystem.
- **Question answering feeds it.** A durable fact from a run is condensed, anchored and merged off
  the critical path. → [Question Answering](features-wiki-qa.md)
- **You and your agents can teach it.** Submit a fact the code alone will not reveal and Mewbo
  condenses it, drops duplicates, and merges it. Every later answer draws on the accumulated set.

> [!TIP] Contribute an insight
> Your agents take that last path as they work, through the [MCP server](clients-mcp.md)'s
> `submit_insight` tool or the REST endpoint.
