# Third-party provenance

GPTgrep's original code is MIT licensed. Dependencies retain their own licenses.
Cargo.lock records the actual resolved graph; the package-local pnpm lock records
the optional incur wrapper graph. Binary archives must carry the bundled dependency
license inventory and PDFium's full native notices, not only this summary.

| Source | Pinned revision/version | Use | License |
| --- | --- | --- | --- |
| [Microsoft tgrep](https://github.com/microsoft/tgrep/tree/239711cfb6e69e8780cabf912a8987162a223ff1) | `239711cfb6e69e8780cabf912a8987162a223ff1` | Embedded tgrep-core; no upstream server | MIT |
| [LiteParse](https://github.com/run-llama/liteparse/tree/a7105d3e3cfaf001970578c6615f6e71015aa483) | `a7105d3e3cfaf001970578c6615f6e71015aa483` | Native parser library, 2.14.6 | Apache-2.0 |
| [PageIndex](https://github.com/VectifyAI/PageIndex/tree/9c4c3ff2ddd2cbd70501997635578064f2b7c7c3) | `9c4c3ff2ddd2cbd70501997635578064f2b7c7c3` | Studied structural semantics; custom Rust implementation, partial algorithm coverage | MIT |
| [incur](https://github.com/wevm/incur) | published `0.5.1` | Optional gptgrep-ai wrapper | MIT |
| [PDFium native binaries](https://github.com/run-llama/pdfium-binaries/releases/tag/chromium%2F8058) | `chromium/8058` | Runtime PDFium library | Per-component licenses bundled in release |
| [Codex](https://github.com/openai/codex) | Runtime selected by caller | Optional local stdio app-server, not redistributed | Apache-2.0 source; hosted service terms separate |

The PageIndex and parser crate notices document their source mapping and limitations.
JevGrep, TypeSafe SDK/skills and the example projects were studied for interface and
workflow patterns; GPTgrep implements its own Rust Decisions client. No Jev weights
are distributed. TypeSafe/OpenRouter are hosted providers selected explicitly by
the caller; API credentials and provider service access are not package assets.

The external PageIndex benchmark, jev-eval and open-jev reference checkouts had no
clear repository license declaration in the reviewed snapshots. Their code/data
are not vendored here. GPTgrep's committed eval corpus is project-authored. A local
reference manifest identifies external benchmark inputs without redistributing PDFs.

Research reports separate source/vendor claims from GPTgrep execution evidence.
The initial Rust layout profile does not claim the complete PageIndex Flash algorithm.
