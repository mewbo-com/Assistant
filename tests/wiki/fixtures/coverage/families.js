// Construct-family fixture for JavaScript. `.jsx` shares this grammar, and a
// React `.jsx` module is almost entirely arrow-function consts — so the arrow
// family matters here for the same reason it does in TypeScript.

export class ModuleClass {
  plainMethod() {
    return 1;
  }
  static staticMethod() {}
}

export function moduleFn() {}

export function* generatorFn() {
  yield 1;
}

export const arrowConst = (a) => a + 1;
const unexportedArrowConst = () => {};
export const asyncArrowConst = async () => {};
export const fnExpressionConst = function inner() {};

export class FieldArrowClass {
  fieldArrowMethod = () => {};
}

export function consumer() {
  arrowConst(1);
  unexportedArrowConst();
}
