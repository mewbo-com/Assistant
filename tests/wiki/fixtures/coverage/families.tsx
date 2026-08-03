// Construct-family fixture for .tsx. Its job is narrower than families.ts:
// prove that a file containing JSX is parsed by a grammar that UNDERSTANDS JSX.
// The TypeScript grammar treats `<div>` as a type assertion and fills the tree
// with ERROR nodes, which silently costs every capture below it — so a fixture
// whose symbols sit AFTER the first JSX expression is the tripwire.
import * as React from "react";

export const ArrowComponent = (): React.ReactElement => <div>hi</div>;

export const useHook = () => {
  const [value, setValue] = React.useState(0);
  return { value, setValue };
};

export function FunctionComponent(): React.ReactElement {
  return <ArrowComponent />;
}

export interface ComponentProps {
  n: number;
}

export type PropsAlias = ComponentProps;

export class ComponentClass extends React.Component<ComponentProps> {
  renderBody(): React.ReactElement {
    return <span>{this.props.n}</span>;
  }
}

// Deliberately last: under the TypeScript grammar the JSX above derails the
// parse, so anything down here disappears. Under the tsx grammar it survives.
export const trailingArrow = (): number => 1;

export function trailingFn(): void {}
