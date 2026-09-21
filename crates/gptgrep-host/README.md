# gptgrep-host

Local Codex workflows over GPTgrep's mandatory Jev retrieval stage.

`ask(root, question, config)` runs a host-owned hybrid search with Jev document routing and evidence reranking **before starting Codex**. Its bounded source evidence is included in the initial model input. The model cannot skip that stage by choosing catalog, tree, or read tools. `summarize(root, node_id, config)` derives the document path and section title from the selected node and constrains its initial search to that document. A conflicting `HostConfig.document` is rejected.

The caller must export `OPENROUTER_API_KEY` for Jev. Missing credentials, provider failure, invalid provider output, missing candidates, or failed seed delivery stop the retrieval workflow. There is no local-only fallback. A real rerank that filters every candidate can continue with an explicit `filtered_all` status and empty evidence. An empty or entirely stale candidate scope cannot be reported as successful reranking.

Further `gptgrep_search` calls default to `hybrid`; `semantic` selects the semantic lane. Explicit `regex` and `lexical` calls are local refinements after the required initial pass. `HostConfig.document` scopes catalog, tree, read, and search access to an indexed relative source path. An additional search cannot widen that scope. Questions and scope come from runtime inputs; indexing receives no questions or expected answers.

The reader defaults to `gpt-5.6-luna`, reasoning effort `max`, with 180 seconds and 12 model-requested dynamic tool calls. The host-owned initial search has its own receipt and is outside that dynamic-call count. One deadline covers initial Jev work and the later Codex workflow; bounded process cleanup can follow the deadline. `HostReport.elapsed_ms` includes initial retrieval, Codex work, cleanup, and final citation checks. `HostConfig.jev_model` selects the Jev model; `None` uses the Jev client's default. Authentication stays with the selected existing `CODEX_HOME`; the host never reads credential files or initiates login. The OpenRouter key remains in the parent process and is removed from the Codex child.

`gptgrep_read` accepts `offset_bytes`, default `0`, relative to the selected node's canonical UTF-8 text. Results contain `node_offset`, `next_offset`, and the evidence window. Continue with the same node ID and the returned `next_offset`; `null` means EOF. The byte budget remains 1–6144, default 4096. Offsets must be UTF-8 boundaries. An empty EOF response supplies no new citable evidence. On `tool_output_limit`, retry the same offset with fewer bytes.

Citations retain each delivered window's offset, continuation cursor, exact byte interval and excerpt hash. Final validation rereads that exact offset and length, including windows beyond 64 KiB, and checks current source/generation identity. Host search uses zero surrounding lines so citable evidence stays inside its node; native CLI context behavior remains separate. Snapshot identity validation does not establish semantic entailment.

## Jev accounting and private runtime records

`ToolReceipt.search` retains each search's mode, required-initial flag, query hash, document scope, generation, raw core metrics/coverage, delivered hit count and output truncation. `HostReport.jev` aggregates:

- `required: true` and `initial_status`;
- `requests`: successful validated Jev responses;
- `attempted_calls`: logical client calls, not proof that HTTP was transmitted;
- `unobserved_attempts`: attempted calls without validated responses;
- actual returned `models`, `usage`, `accounting_complete`, and per-search `searches`.

Unknown usage and cost remain unavailable. Coverage is null until the core supplies an observed snapshot. Progress is cumulative within a search: accounting replaces the prior snapshot and aggregates once across distinct searches.

Before provider work, the host creates a private, create-new ledger under `.gptgrep/host-attempts/`. It rejects symlinks and path escapes. On Unix the directory is 0700 when created and files are 0600. This is a local metadata write; read-only mode describes source access and the Codex child, not an absence of runtime receipts. The JSONL ledger is limited to 4 MiB, 1024 events, and 64 KiB per event. Entries are synced before/after client calls and retain stage, hashes, scope, model/usage metadata and bounded receipt summaries—not source text, questions, credentials, or child stderr.

The ledger preserves completed routing usage if reranking fails or is cancelled. Interrupted workflows leave incomplete records. `HostRetrievalError` is serializable and contains `code`, `stage`, `generation`, `ledger_path`, `jev`, projected `receipts`, optional sanitized core `cause`, and `elapsed_ms`. Later Codex or citation failures retain already incurred Jev observations and remain errors. These records and native identities are private runtime evidence, not public release artifacts.

`HostConfig.trace_path` optionally adds a separate bounded method/ID/tool-name-only protocol trace. On Unix, cleanup targets a freshly created owned process group, including launcher descendants, and reaps its leader.

## Independent JSON completion

`complete_json(instructions, state, schema, config)` is a separate no-native-tools completion primitive for source pipeline adapters. It needs no index, adds no Jev stage or attempt ledger, and returns `CompletionReport.value` plus native provenance/usage. Its input budget defaults to 256 KiB with a 1 MiB hard cap; output is 128 KiB. Caller schemas are preserved and validated without external resolution or type coercion. Caller-schema tool selections or action proposals may be returned as JSON data for an outer executor; no native tool or external resource is invoked. This primitive makes no retrieval, citation, or semantic acceptance claim.

See the public [architecture](../../docs/architecture.md) and [portable reader profile](../../config/codex-host.toml). The profile is not automatically installed globally.

```sh
cargo test -p gptgrep-host
cargo clippy -p gptgrep-host --all-targets -- -D warnings
```

Tests use synthetic documents, in-memory Codex protocol transports, and loopback Jev HTTP fixtures. They do not call a paid provider.
