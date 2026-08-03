/**
 * The generative-UI allowlist — the security boundary for model-authored UI.
 *
 * A component name reaches the DOM only by appearing as a key here. Anything
 * else resolves to the fallback placeholder (`GenerativeUIFallbackNode`), so
 * the blast radius of a model naming a component we don't ship is one row.
 *
 * This map lives in its own `.ts` file rather than beside the adapters on
 * purpose: `react-refresh/only-export-components` warns when a `.tsx` exports
 * both components and a non-component value, `allowConstantExport` covers only
 * literals (not an object), and `lint:ci` runs at `--max-warnings=0`.
 *
 * Vocabulary v1 is exactly these eleven names. A twelfth is a product
 * decision, not an implementation detail.
 */
import type { GenerativeUIComponentRegistry } from "@assistant-ui/react";

import {
  AlertNode,
  BadgeNode,
  CardNode,
  CodeBlockNode,
  DividerNode,
  HeadingNode,
  KeyValueNode,
  LinkNode,
  StackNode,
  TableNode,
  TextNode,
} from "./nodes";

export const GENERATIVE_UI_COMPONENTS: GenerativeUIComponentRegistry = {
  Text: TextNode,
  Heading: HeadingNode,
  Card: CardNode,
  Stack: StackNode,
  Badge: BadgeNode,
  KeyValue: KeyValueNode,
  Table: TableNode,
  CodeBlock: CodeBlockNode,
  Alert: AlertNode,
  Divider: DividerNode,
  Link: LinkNode,
};
