# PageIndex Flash stage coverage

Baseline: [VectifyAI/PageIndex at 9c4c3ff](https://github.com/VectifyAI/PageIndex/tree/9c4c3ff2ddd2cbd70501997635578064f2b7c7c3).
“Verified” below describes the named bounded tests. It is not a claim of full
Flash parity, corpus-wide retrieval quality or another platform's acceptance.

| Stage | Implementation status | Verification and remaining boundary |
|---|---|---|
| PDF native extraction, font metadata, viewport geometry | Alternate implementation: pinned LiteParse/PDFium Rust crates | Real generated two-page and 1,001-page PDFs verify ingestion and page citations; no character-for-character Flash extractor parity |
| Unicode/font repair, Type-3 cross-page geometry | Flash-specific port planned; LiteParse applies its own logic | Needs differential font/encoding/rotation corpus |
| Span/line clustering, column gutters, line-number removal | Flash-specific port planned; LiteParse projection is the current alternate | No Flash stage parity claim |
| Document-wide style statistics and block classification | Flash-specific port planned; LiteParse layout blocks currently supply headings | Parser batches are bounded; full-document Flash statistics are not reproduced |
| Header/footer, watermark, boilerplate, TOC and caption classification | Flash-specific port planned; some alternate behavior exists in LiteParse | No equivalence inferred from similar output |
| Document title selection, heading candidates/cliques and hierarchy stack | Alternate LiteParse headings plus GPTgrep deterministic hierarchy | Unit tests verify hierarchy, source location and complete spans; full Flash candidate selection remains planned |
| Plaintext Markdown hierarchy | GPTgrep native CommonMark implementation | Tests verify ATX/Setext, Unicode, metadata/code exclusion and exact source lines; separate from the PDF Flash algorithm |
| Bookmark validation and framing | Partial structural adaptation | Tests verify backward/out-of-range/generic rejection, frame insertion and line/page boundaries; FULL/SKELETON/IGNORE tier thresholds and full graft/repair logic remain planned |
| Preface, inclusive spans, parent promotion and flat fallback | Implemented with documented GPTgrep policy | Coverage, same-page and blank-page tests pass; GPTgrep retains page fallback beyond upstream managed mode's ten-page refusal |
| `S`, residual-page union, worst-case `C` and independent frontier cost | Ported deterministic stage | Eleven source-generated differential cases check exact upstream numeric results and frontier equivalence |
| Same-span leaf sibling merge and title fallback | Ported, with source-line union extension | Differential cases cover single/multiple-page spans, metadata, Unicode-character title limits and long-title fallback; line-union test preserves evidence |
| Bottom-up cost merge, ties, retained descendant titles and ID relabeling | Ported, with pre-existing parent metadata retained | Differential outputs match pinned source on eleven cases; idempotence, source coverage, parent containment and malformed-input tests pass |
| LLM expansion and summaries | Not implemented by this crate | A host/provider integration must be independently tested; no expansion or summary claim |
| Local SDK storage, agent reasoning/tree traversal and answer citations | Outside this crate; GPTgrep core/host owns workflow integration | Neither tree shape nor deterministic merge tests establish SDK/retrieval parity |
| End-to-end PageIndex benchmark quality | Planned comparative evaluation | Must pin corpus, questions, index model, reasoning model, OCR policy, budgets, evidence correctness and cost; upstream scores are not GPTgrep results |

The public merge API is opt-in. Plaintext has one virtual page, so physical-page
costs can merge its whole hierarchy; removed headings remain in `key_items`.
The native parser's default behavior is unchanged. Source mappings, licensing,
the differential fixture generator and exact source SHA-256 are recorded in
[THIRD_PARTY.md](THIRD_PARTY.md).
