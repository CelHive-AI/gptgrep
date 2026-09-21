# GPTgrep

GPTgrep is a Rust-first, local document retrieval CLI for agents. It combines
trigram-filtered regex verification, document structure, and optional Jev judgments.
The primary agent retains planning and reasoning; the helper returns source evidence.
There is no vector database and no MCP server.

## User contract

- Index an explicit document directory, preserving exact source hashes and page/line
  coordinates. Plaintext, Markdown and MDX retain source-line identity. Other
  supported formats use LiteParse extraction and page-based citations.
- Provide native grep-style commands, bounded JSON output, an inspectable command
  schema, document trees and selected-node reads for programmatic tool composition.
- Use a pinned tgrep-core package for lexical candidate filtering. Verify actual
  regex matches after filtering, including patterns that cannot use trigrams.
- Add Jev through its typed Decisions API for semantic document routing and
  candidate reranking. Local search must work without credentials or a network.
- Keep exact, lexical and model-assisted results distinguishable. Report budgets,
  truncated candidate coverage, actual provider identity and unavailable metrics.
- Treat documents as data. Do not execute document instructions, start services,
  silently ingest hidden credentials, or send document text to a provider unless
  the caller explicitly selects a model-assisted command/mode.

## Initial release acceptance

1. A real mixed document fixture can be parsed, indexed, searched and cited end to
   end by the release build. Tree spans are valid and deterministic.
2. Indexed regex results agree with direct regex scanning on controlled cases;
   MatchAll, Unicode, literal and case-insensitive queries are covered.
3. A stale or deleted source cannot appear as fresh evidence. A failed rebuild
   cannot replace the current generation. Readers see a complete snapshot.
4. A synthetic live Jev run validates the current gateway contract and records
   actual model, latency, usage and cost when returned. Missing data stays null.
5. Offline evals report retrieval quality and citation fidelity on pinned fixtures.
   Source-reported benchmark numbers remain separate from GPTgrep measurements.
6. The optional incur interface is tested as a bounded native-binary wrapper with
   schema discovery and no MCP serving. The native CLI remains independently usable.
7. Source provenance/licenses, independent review, checks, Git/PoUW, local backup
   and GitHub source/release delivery are recorded separately.

## Scope and research boundary

The first release is an evaluated development baseline, not a claim of complete
PageIndex Flash parity or universally superior RAG. Flash contains a substantial
layout pipeline and optional generative optimization. The initial Rust crates use
LiteParse layout plus documented structural PageIndex rules. Parity and scale
claims require a future comparative corpus and measured acceptance.

The project may evolve autonomously within this direction. New global registrations,
automatic ingestion of private histories, changes to other projects, and broad
model-training/research experiments are outside this repository's routine authority.

## Long-horizon research goal

The research-preview program must demonstrate a useful measured advantage over a
live PageIndex Flash baseline on the pinned PageIndex-OSS-Benchmark tasks. A source
release, passing process, synthetic smoke or GPTgrep-only corpus run does not meet
this goal. The same corpus/task cohort, model/effort, source revisions, prompt and
tool budgets, judge and failure denominators must be retained for each comparison.
Provider/backend substitutions and harness differences must be explicit.

Discover new issues from live failures, propose one falsifiable improvement, run
paired ablations, retain failed outcomes, and promote only verified improvements.
Use a small paired smoke cohort first, then the complete 62-task/34-document cohort
after the pipeline is reliable. Do not tune on held-out test answers or relabel
failures as excluded successes. Report quality, citation fidelity, ingestion
coverage, latency, token usage and available cost as separate measures.

The comparison contract is `workspace/harness-config/goal-contract.json`. The
native outer Goal remains active until that contract's actual acceptance is met.
