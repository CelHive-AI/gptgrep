# gptgrep-pageindex

A pure Rust document tree and citation layer for GPTgrep. It has no model,
Python, server, or PDFium dependency.

`from_text(title, text)` preserves the UTF-8 source text exactly. It recognizes
CommonMark ATX and Setext headings, including nested section levels, and skips
code blocks, metadata frontmatter, list content and block quotations. Text before
the first section gets a Preface node; headingless text gets a whole-file node.
An empty file is valid and has no nodes or pages. Its parser identifier is exactly
`plaintext`, so its one-based inclusive line ranges are source-file citations.

`from_pages(title, pages, headings, bookmarks, parser)` builds an extracted-text
tree from complete, consecutive physical pages. Each page gets at least one
canonical line; newline conventions are normalized, and pages are concatenated
with a terminating newline. Layout headings carry verified page-relative line
locations. Valid bookmarks supply a hierarchy frame, with absent layout headings
inserted below the active frame. Backward, out-of-bounds, empty and generic
page-number bookmarks are ignored with a warning. A bookmark without a located
heading has page-only precision: the preceding section conservatively includes
its boundary page. Blank pages remain represented. Without a hierarchy, each
page becomes a leaf. `locate_heading` matches an existing title against at most
eight consecutive extracted lines; it never invents missing text.

The serializable types are `ParsedDocument`, `Page` and `TreeNode`. Node IDs are
deterministic document-local preorder IDs, not persistent global identifiers.
Flat nodes use `parent_id` to preserve hierarchy. Parent ranges include children;
ranges may overlap. All non-empty ranges are one-based and inclusive. Extracted
line ranges address `ParsedDocument.text`; physical PDF page numbers supplement
them. Printed PDF page labels are not physical page ordinals.

`optimize_merge(&document)` is an opt-in deterministic port of PageIndex's merge
operators. It first combines leaf siblings covering identical page spans, then
visits subtrees bottom-up. A leaf costs its inclusive page span `S`; an expanded
node costs `1 + max(residual parent pages, child costs)`. A subtree collapses when
`S <= cost`, including ties. Overlapping children cover a union of pages, so they
are never double-counted. Operators run to a fixed point and IDs are relabeled
in preorder. `TreeNode.key_items` retains removed headings and existing metadata;
the field defaults to an empty list when reading older serialized indexes.

The optimizer returns a new document and validates parent containment, physical
page boundaries, source line ranges and complete coverage. Same-span siblings
union their line ranges; all source text and pages remain unchanged. It runs no
model, summary generator, or expansion pass. The cost unit is a physical page:
plaintext currently has one virtual page, so applying it to plaintext can
collapse its whole heading hierarchy. Keep that mode opt-in and retain
`key_items` in routing context. Native parsing itself does not invoke optimization.

This is a **LiteParse-derived layout profile with PageIndex-inspired range
semantics**. It does not implement full PageIndex Flash parity, its multilingual
layout classifier, complete bookmark tier/grafting algorithm, summaries, or
model-based expansion. Its deterministic cost/merge stage is independently
ported and verified. It also allows an explicitly reported page fallback
for documents longer than PageIndex SDK's ten-page flat-tree limit. Those are
separate, testable future work; no parity or retrieval benchmark claim is made.

Run `cargo test -p gptgrep-pageindex` from the workspace root. Tests cover source
line preservation, frontmatter/code exclusion, Unicode/Setext hierarchy, empty
files, same-page headings, blank-page coverage, coarse bookmark boundaries and
invalid coordinates. Eleven differential fixtures execute the pinned upstream
pure merge operators to establish cost and output expectations; Rust tests check
those outputs, the independent frontier cost formulation, fixed-point
idempotence, retained metadata, line unions and invalid input rejection.
Regenerate with the supplied fixture generator and the exact pinned upstream
`pageindex/tree_optimize.py`; its SHA-256 guard refuses a different source.
See [FLASH_STAGE_COVERAGE.md](FLASH_STAGE_COVERAGE.md) for stage-by-stage status
and [THIRD_PARTY.md](THIRD_PARTY.md) for attribution and the small documented
differences from upstream.
