;; typescript.scm — tree-sitter capture patterns for the wiki graph
;; Extends JS patterns with interface support. TypeScript uses type_identifier
;; for class/interface names instead of identifier.

; Top-level classes — TypeScript uses type_identifier for class names
(class_declaration
  name: (type_identifier) @class.name) @class.def

; Top-level functions
(function_declaration
  name: (identifier) @function.name) @function.def

; Methods inside class body
(class_declaration
  body: (class_body
          (method_definition
            name: (property_identifier) @method.name) @method.def))

; Class heritage — `class Sub extends Base`
(class_declaration
  name: (type_identifier) @subclass.name
  (class_heritage
    (extends_clause
      value: (identifier) @superclass.name)))

; Abstract classes are their OWN node type, so the pattern above never sees one.
(abstract_class_declaration
  name: (type_identifier) @class.name) @class.def @class.subkind.abstract

; Enum → Class + subkind, the same mapping java.scm uses.
(enum_declaration
  name: (identifier) @class.name) @class.def @class.subkind.enum

; Interfaces — `interface Foo { ... }` (may be inside export_statement)
(interface_declaration
  name: (type_identifier) @interface.name) @interface.def

; Type aliases — TypeScript's other declaration surface, and 239 of them in this
; repository against 381 interfaces. Modeled as Interface + a subkind rather than
; a new node kind: `type Props = {…}` and `interface Props {…}` are
; interchangeable at most call sites, and schema v2 keeps `kind` structural and
; closed while `subkind` carries linguistic flavour.
(type_alias_declaration
  name: (type_identifier) @interface.name) @interface.def @interface.subkind.type_alias

; Arrow-function and function-expression consts. In a TS/React codebase this is
; the DOMINANT way a component, hook or helper is declared — `export const Foo =
; () => …` — and with no pattern for it a module of nothing but arrows yielded no
; symbols at all. The `variable_declarator` is the def; its byte range starts at
; the name, which is the token a reader points at. A destructuring `const {a} =
; …` has an `object_pattern` name and deliberately does not match: it binds
; values, it does not declare one symbol.
(variable_declarator
  name: (identifier) @function.name
  value: [(arrow_function) (function_expression)]) @function.def

; Generator functions — `function* gen()` is its own node type.
(generator_function_declaration
  name: (identifier) @function.name) @function.def

; DELIBERATELY NOT CAPTURED: a declaration wrapped in a call, `const Input =
; React.forwardRef((props, ref) => …)`. Those are real components, but at the
; grammar level they are indistinguishable from `const rows = useMemo(() => …)`
; — and in this repository the general shape is 477 declarators, only 76 of
; which (56 forwardRef, 20 lazy) declare anything; the other 401 are
; useMemo/useCallback/filter locals. Capturing the shape buries the symbols it
; finds. Narrowing it instead means hardcoding React's function names into a
; language-level query file, which puts library knowledge at the grammar layer.
; The cost is that a handful of forwardRef-wrapped UI primitives report no
; symbols; that is the known, accepted gap.

; A class field holding an arrow is a method in all but syntax (the React
; class-component `handleClick = () => {}` idiom). A field holding anything else
; is data, and is deliberately NOT captured: TypeScript has no Property family
; here, and minting a symbol per config constant would bury the real ones.
(public_field_definition
  name: (property_identifier) @method.name
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
