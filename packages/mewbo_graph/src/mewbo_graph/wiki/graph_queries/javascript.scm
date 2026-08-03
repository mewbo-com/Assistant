;; javascript.scm — tree-sitter capture patterns for the wiki graph

; Top-level classes
(class_declaration
  name: (identifier) @class.name) @class.def

; Top-level functions
(function_declaration
  name: (identifier) @function.name) @function.def

; Methods inside class body
(class_declaration
  body: (class_body
          (method_definition
            name: (property_identifier) @method.name) @method.def))

; Class heritage (EXTENDS edge) — `class Sub extends Base`
(class_declaration
  name: (identifier) @subclass.name
  (class_heritage
    (identifier) @superclass.name))

; Arrow-function and function-expression consts. `.jsx` shares this grammar,
; and a React `.jsx` module is almost entirely `const Foo = () => …` — the same
; reason typescript.scm carries this pattern; see the longer note there.
(variable_declarator
  name: (identifier) @function.name
  value: [(arrow_function) (function_expression)]) @function.def

; Generator functions — `function* gen()` is its own node type.
(generator_function_declaration
  name: (identifier) @function.name) @function.def

; A class field holding an arrow is a method in all but syntax. JavaScript names
; this node `field_definition` with a `property:` field, where TypeScript uses
; `public_field_definition` with `name:` — the two grammars genuinely differ, so
; the pattern cannot simply be shared.
(field_definition
  property: (property_identifier) @method.name
  value: [(arrow_function) (function_expression)]) @method.def

; ES6 imports — `import x from 'mod'` / `import { y } from 'mod'`
(import_statement
  source: (string) @import.module)

; Calls — `foo()` or `obj.bar()`
(call_expression
  function: [
    (identifier) @call.name
    (member_expression
      property: (property_identifier) @call.name)
  ])
