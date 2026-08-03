// Construct-family fixture: every shape of TypeScript declaration the graph
// models. Symbol names encode their family so a failure names the pattern.

export class ModuleClass {
  plainMethod(): number {
    return 1;
  }
  static staticMethod(): void {}
  get getterMethod(): number {
    return 1;
  }
}

export abstract class AbstractClass {
  abstract abstractMethod(): void;
}

export class DerivedClass extends AbstractClass {
  abstractMethod(): void {}
}

export function moduleFn(): void {}

export function* generatorFn(): Generator<number> {
  yield 1;
}

export interface ModuleInterface {
  field: string;
}

// Type aliases — a TS declaration surface with no node kind of its own.
export type ExportedAlias = { a: string };
type LocalAlias = "x" | "y";
export type GenericAlias<T> = T[];

export enum ModuleEnum {
  Red,
  Green,
}

// Arrow-function and function-expression consts.
export const arrowConst = (a: number): number => a + 1;
const unexportedArrowConst = (): void => {};
export const asyncArrowConst = async (): Promise<void> => {};
export const fnExpressionConst = function inner(): void {};

// A class field holding an arrow is a method in all but syntax.
export class FieldArrowClass {
  fieldArrowMethod = (): void => {};
  static staticFieldArrowMethod = (): void => {};
}

export function consumer(): void {
  arrowConst(1);
  unexportedArrowConst();
  asyncArrowConst();
  fnExpressionConst();
}
