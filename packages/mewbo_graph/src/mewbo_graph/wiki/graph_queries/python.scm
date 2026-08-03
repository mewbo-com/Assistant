;; python.scm — tree-sitter capture patterns for the wiki graph

; Every class, at any depth.
;
; These three families used to be anchored to a parent shape — `(module
; (class_definition …))`, `(module (function_definition …))`, and
; `(class_definition body: (block (function_definition …)))`. That is what made
; a DECORATED def invisible: `@property def x()` parses as `(decorated_definition
; (function_definition …))`, so the def is a child of the decorator wrapper and
; never a direct child of `module` or of a class `block`. Nested defs and defs
; under an `if`/`try` guard were lost the same way. Anchoring cannot be repaired
; by listing more parents — tree-sitter matches a FIXED ancestor shape and has no
; descendant axis, so every shape nobody enumerated stays silently absent.
(class_definition
  name: (identifier) @class.name) @class.def

; Every def, at any depth, decorated or not.
;
; Whether one is a Function or a Method is a question about its NEAREST
; enclosing definition, which is exactly what a query cannot ask — so this ONE
; family carries both and `graph.py:_split_callables_by_scope` divides it by byte
; containment.
;
; Capture the inner `function_definition`, NOT the `decorated_definition`
; wrapper, for three reasons. `_extract_docstring` reads a `block` child, which
; the wrapper does not have. The def's byte range should be the definition, with
; decorators as metadata. And `node_id` folds `start_byte`: keeping the inner
; node's byte means every symbol these patterns ALREADY reached keeps the id it
; already had, so widening coverage adds rows without orphaning existing ones.
(function_definition
  name: (identifier) @callable.name) @callable.def

; Class heritage (EXTENDS edge)
(class_definition
  name: (identifier) @subclass.name
  superclasses: (argument_list
                  (identifier) @superclass.name))

; Plain `import x` and `import x.y`
(import_statement
  name: (dotted_name) @import.module)

; `from x import y, z`
(import_from_statement
  module_name: (dotted_name) @import.from_module
  name: (dotted_name) @import.from_name)

; Direct calls — `foo()` or `obj.bar()`
(call
  function: [
    (identifier) @call.name
    (attribute attribute: (identifier) @call.name)
  ])
