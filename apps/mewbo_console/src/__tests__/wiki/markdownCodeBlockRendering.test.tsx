/**
 * The `code()` component in `markdownComponents.tsx` used to stringify its
 * `children` before rendering a highlighted block. rehype-highlight replaces
 * a fenced block's single text child with an array of per-token `<span>`
 * elements, so `String(children)` stringified each token to `[object
 * Object]`. A test that hands the component a plain-string child (bypassing
 * rehype-highlight) cannot reproduce this — these tests render through the
 * real `react-markdown` + `remarkGfm` + `rehypeHighlight` pipeline, which is
 * the only way to get an element-array `children` onto the component.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";

import { buildMarkdownComponents } from "@/components/wiki/markdownComponents";

afterEach(cleanup);

function noNav(): void {
  return;
}

const PYTHON_SOURCE = [
  "async def handler(prompt):",
  "    result = await loop.run(prompt)",
  '    if result.state not in {"a", "b"}:',
  '        raise RuntimeError("boom")',
  "    return result",
].join("\n");

function renderMarkdown(body: string, enableMermaid = false) {
  const components = buildMarkdownComponents({ onNavigatePage: noNav, enableMermaid });
  return render(
    <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]} components={components}>
      {body}
    </ReactMarkdown>,
  );
}

describe("wiki code blocks through the real rehype-highlight pipeline", () => {
  it("renders a highlighted python fence as readable source, not [object Object] soup", () => {
    const { container } = renderMarkdown("```python\n" + PYTHON_SOURCE + "\n```\n");

    expect(container.textContent).not.toContain("[object Object]");
    expect(container.textContent).toContain("async");
    expect(container.textContent).toContain("RuntimeError");
    expect(container.textContent).toContain('result.state not in {"a", "b"}');

    // rehype-highlight actually tokenised the block, proving the assertions
    // above exercised the span-array path and not a plain string child.
    expect(container.querySelectorAll(".hljs-keyword").length).toBeGreaterThan(0);
  });

  it("renders the same fence correctly in the LiveBlocks (Q&A) configuration", () => {
    const { container } = renderMarkdown("```python\n" + PYTHON_SOURCE + "\n```\n", false);

    expect(container.textContent).not.toContain("[object Object]");
    expect(container.textContent).toContain("RuntimeError");
  });

  it("still renders inline code (never stringified) unaffected", () => {
    renderMarkdown("call `handler(prompt)` directly");
    expect(screen.getByText("handler(prompt)")).toBeInTheDocument();
  });

  it("gives an unlabeled fence the hljs base class even though rehype-highlight leaves it alone", () => {
    const { container } = renderMarkdown("```\nplain fence, no language\n```\n");
    const code = container.querySelector("pre code");
    expect(code).not.toBeNull();
    expect(code?.className).toContain("hljs");
    expect(container.textContent).toContain("plain fence, no language");
  });

  it("passes an uncorrupted mermaid source through to MermaidBlock", async () => {
    vi.resetModules();
    vi.doMock("@/components/wiki/MermaidBlock", () => ({
      MermaidBlock: ({ inlineSource }: { inlineSource: string }) => (
        <div data-testid="mermaid-source">{inlineSource}</div>
      ),
    }));
    const { buildMarkdownComponents: buildWithMockedMermaid } = await import(
      "@/components/wiki/markdownComponents"
    );
    const components = buildWithMockedMermaid({ onNavigatePage: noNav, enableMermaid: true });
    const mermaidSource = "graph TD\n  A --> B\n  B --> C";
    render(
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]} components={components}>
        {"```mermaid\n" + mermaidSource + "\n```\n"}
      </ReactMarkdown>,
    );
    const captured = screen.getByTestId("mermaid-source");
    expect(captured.textContent).toBe(mermaidSource);
    expect(captured.textContent).not.toContain("[object Object]");
    vi.doUnmock("@/components/wiki/MermaidBlock");
  });
});
