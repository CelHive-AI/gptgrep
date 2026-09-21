# gptgrep-host

Optional local Codex app-server workflows over GPTgrep's existing document snapshots.

`ask(root, question, config)` performs model-backed, multi-step retrieval. `summarize(root, node_id, config)` reads and summarizes a selected node with optional tree expansion. Both return `HostReport` with actual thread/turn IDs, requested and server-reported model/effort, checked citations, bounded tool receipts and usage when available.

Defaults are `gpt-5.6-luna`, reasoning effort `max`, 180 seconds, and 12 dynamic tool calls. Authentication stays with the selected existing `CODEX_HOME`; this crate does not read credential files or initiate login. It spawns a private stdio child, never a daemon or socket server.

The host serves only local catalog, tree, lexical/regex search and node-read functions. It rejects citations not issued by search/read, changed generations and stale source hashes. Snapshot identity validation is separate from semantic entailment.

`gptgrep_read` accepts `offset_bytes`, default `0`, relative to the selected node's canonical UTF-8 text. Each result includes `node_offset`, `next_offset`, and the evidence window. Pass the returned `next_offset` as the next `offset_bytes` with the same `node_id`; `null` means the end of the node. The byte budget remains 1–6144, default 4096. Offsets must be UTF-8 boundaries; use the returned cursor instead of counting characters. An empty end-of-node result supplies no new citable evidence.

For example, a result with `next_offset: 4096` continues with `{"node_id":"<issued node ID>","offset_bytes":4096}`. On `tool_output_limit`, retry the same offset with fewer bytes. A search hit's `node_offset` can also start a focused node read. Host search uses zero surrounding lines so its evidence stays inside the selected node; native CLI context behavior is separate.

Citations retain each delivered window's offset, continuation cursor, exact byte interval and excerpt hash. Final validation rereads that exact offset and byte length, including windows beyond 64 KiB, and checks current source/generation identity. Reading an entire large node still consumes the configured tool-call budget; continuation does not imply that unseen windows have been read.

See [the protocol and permission research](../../docs/research/codex-host.md) and [portable profile template](../../config/codex-host.toml). The profile template is not automatically installed globally; the caller controls runtime home selection.

```sh
cargo test -p gptgrep-host
cargo clippy -p gptgrep-host --all-targets -- -D warnings
```

Tests use in-memory mock protocol transports and synthetic local documents. They do not contact a model.



`complete_json(instructions, state, schema, config)` is a separate no-tools JSON completion primitive for source pipeline adapters. It needs no index and returns `CompletionReport.value` plus native provenance/usage. The input budget defaults to 256 KiB and has a 1 MiB hard cap; output is 128 KiB. Caller schemas are preserved and validated without external resolution or type coercion. No citation/semantic acceptance is inferred for pure completions.

Pure completions may return caller-schema tool selections or action proposals as JSON data for an outer executor. They cannot invoke native Codex tools or access external resources. Describing a proposed action in structured output is distinct from executing it.

Set `HostConfig.trace_path` for an optional new private metadata-only JSONL trace. On Unix, timeout/cancellation cleanup targets a freshly created owned process group and includes launcher descendants. An ask/summarize turn with no successful search/read fails with `host_no_evidence_tools`.
