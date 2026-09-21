# GPTgrep

GPTgrep is a Rust-first, local document retrieval CLI for agents. It combines
trigram-filtered regex verification, document structure, and required Jev routing
and reranking in its default retrieval and reasoning workflows.
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
- Use Jev through its typed Decisions API for semantic document routing and
  candidate reranking. Default search, ask and summarize require this stage and
  fail explicitly if its credentials or service are unavailable. Explicit regex
  and lexical primitives remain available locally for exact inspection and
  controlled ablations; they do not constitute the complete GPTgrep workflow.
- Keep exact, lexical and model-assisted results distinguishable. Report budgets,
  truncated candidate coverage, actual provider identity and unavailable metrics.
- Treat documents as data. Do not execute document instructions, start services,
  silently ingest hidden credentials, or send document text to a provider unless
  the caller invokes a documented model-assisted command. Default search, ask and
  summarize are model-assisted; parse, index and explicit regex/lexical are local.

## Initial release acceptance

The first experimental publication requires the long-horizon comparison contract
below: a real, reproducible minimum advantage over PageIndex Flash plus GPT-5.6.
The engineering checks in this section are necessary and do not replace that gate.

1. A real mixed document fixture can be parsed, indexed, searched and cited end to
   end by the release build. Tree spans are valid and deterministic.
2. Indexed regex results agree with direct regex scanning on controlled cases;
   MatchAll, Unicode, literal and case-insensitive queries are covered.
3. A stale or deleted source cannot appear as fresh evidence. A failed rebuild
   cannot replace the current generation. Readers see a complete snapshot.
4. Live default search and local Codex workflows actually execute required Jev
   document routing and candidate reranking, record actual model, request count,
   latency, usage and cost when returned, and preserve failures. Missing data stays
   null. A standalone provider transport smoke cannot satisfy this workflow gate.
5. Offline evals report retrieval quality and citation fidelity on pinned fixtures.
   Source-reported benchmark numbers remain separate from GPTgrep measurements.
6. The optional incur interface is tested as a bounded native-binary wrapper with
   schema discovery and no MCP serving. The native CLI remains independently usable.
7. Source provenance/licenses, independent review, checks, Git/PoUW, local backup
   and GitHub source/release delivery are recorded separately.

## Scope and research boundary

The first release must satisfy its scoped comparative gate; it does not claim
complete PageIndex Flash parity or universally superior RAG. Flash contains a substantial
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

The harness also measures a named, pinned conventional embedding-RAG comparator
with declared chunking, encoder, similarity metric and context budget. It is an
evaluation dependency, not part of GPTgrep's vectorless runtime. Use the same task
scope, reader and judge profiles where the experiment controls them. A generic
"standard RAG" label is not a reproducible baseline specification.

Discover new issues from live failures, propose one falsifiable improvement, run
paired ablations, retain failed outcomes, and promote only verified improvements.
Never hardcode benchmark questions, answers, document IDs, annotated evidence
pages, or task-specific routing/response rules in product code or adapters. The
harness loads the pinned dataset at runtime. Retrieval receives only the question,
protocol-permitted scope and document evidence; gold answers and annotations are
reserved for separate judging/scoring. Improvements must use general algorithms
and synthetic regression cases, rather than recognizing evaluation tasks.
This also forbids specialization to benchmark document types, question templates,
answer formats and annotated answer-location distributions. Index construction
receives raw documents alone. Reader prompts and caches must not inherit task
labels, gold annotations or previous judge state. Synthetic metamorphic checks
should vary file names, page placement, section order, question wording and facts
to establish that improvements are general. Protocol-provided document scope is
permitted only when supplied consistently to both systems and clearly labelled.
Run the complete 62-task/34-document cohort as the live A/B benchmark. Offline
fixtures validate implementation; small or synthetic live smokes do not satisfy
the A/B requirement or first-release gate. Do not tune on held-out test answers or relabel
failures as excluded successes. Report quality, citation fidelity, ingestion
coverage, latency, token usage and available cost as separate measures.
Retain per-task retrieval and tool-failure signals so an observed gap can become a
specific issue, falsifiable optimization and paired rerun. Adapter startup or
backend differences alone cannot establish retrieval superiority.

The comparison contract is `workspace/harness-config/goal-contract.json`. The
native outer Goal remains active until that contract's actual acceptance is met.
