# Markdown list boundary test design

## Goal

Document the boundary behavior of the in-app Markdown list renderer after it
was updated to keep loose lists (list items separated by blank lines) together.

## Scope

Add regression coverage only. Do not change the renderer's list-merging
behavior.

## Behavior

- Consecutive same-type list markers, including those separated by blank lines,
  form one list.
- A blank line followed by a different list type ends the current list.
- A blank line followed by ordinary paragraph content ends the current list and
  leaves that content available to the generic paragraph renderer.
- Ordered-list source marker values are not preserved; as with standard
  Markdown, the renderer creates an ordinary `<ol>` and browsers display a
  continuous sequence.

## Tests

Extend the existing Node-backed frontend Markdown renderer test with:

1. A loose ordered list followed by an unordered list, asserting separate
   `<ol>` and `<ul>` output.
2. A loose ordered list followed by paragraph text, asserting the list ends and
   the paragraph content remains rendered.
3. A non-contiguous source numbering case (`1.` then `3.`), asserting one
   ordinary ordered list with two items; this records the intended normalized
   numbering semantics.

## Verification

Run the focused frontend renderer test and then the complete pytest suite.
