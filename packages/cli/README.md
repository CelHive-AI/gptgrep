# Optional incur interface

`gptgrep-ai` uses pinned published `incur` 0.5.1 to expose command schemas and structured formatting around the native Rust `gptgrep` binary. Node 22 or later is required for this optional package. Native `gptgrep` remains independently usable without Node or Bun. The research checkout declares 0.6.0, which was unavailable from the package registry during integration; the wrapper is tested against its actual locked dependency.

Install this package's locked dependencies with `pnpm install --frozen-lockfile`, then run `node src/cli.js`. Put the native binary on PATH or set `GPTGREP_BIN` to its executable path. The wrapper does not install or download the native binary.

```sh
GPTGREP_BIN=/path/to/gptgrep node src/cli.js index --request '{"root":"./docs"}' --json
GPTGREP_BIN=/path/to/gptgrep node src/cli.js search --request '{"query":"--mcp","root":"./docs","mode":"lexical","limit":10}' --json
node src/cli.js search --schema --json
node src/cli.js request-schema search --json
node src/cli.js --llms --format json
```

All forwarding commands accept `--request` with one JSON object. `request-schema COMMAND --json` describes that object's fields. `COMMAND --schema --json` describes the wrapper's environment, options and output. Shell callers should encode the complete JSON object as one argument; programmatic callers should pass an argument array. This keeps query text such as `--json`, `--schema`, and `--mcp` from being interpreted by incur as flags. For ordinary grep positional patterns, use native `gptgrep` directly.

| Command | JSON request fields | Native operation |
|---|---|---|
| `index` | `root` (default `.`), `optimizeMerge` (default false; native paginated documents only) | `gptgrep index ROOT [--optimize-merge] --json` |
| `search` | `query`, `root`, `mode` (`regex`, `lexical`, `hybrid`, `semantic`; default `regex`), `limit` (1–1000; default 20), `context` (0–100; default 0), `minScore` (0–1; default 0.5), `ignoreCase`, `fixedStrings` | `gptgrep search QUERY ROOT --mode MODE --limit N --context C --min-score S --json` |
| `tree` | `file`, `root` | `gptgrep tree FILE --root ROOT --json` |
| `status` | `root` | `gptgrep status ROOT --json` |
| `doctor` | none | `gptgrep doctor --json` |
| `ask` | `question`, `root`, optional host settings below | `gptgrep ask QUESTION ROOT --json` |
| `summarize` | `nodeId`, `root`, optional host settings below | `gptgrep summarize DOCUMENT_ID:NODE_ID --root ROOT --json` |

Search defaults to offline regex with zero context. `hybrid` and `semantic` explicitly use the native Jev provider path. `minScore` is a relevance-rubric floor, not calibrated confidence. `ask` and `summarize` explicitly invoke the native local Codex host, using the existing account environment. Their host settings are `codexBin` (default `codex`), optional `codexHome`, `model` (default `gpt-5.6-luna`), `reasoningEffort` (default `max`), `timeout` (1–900 seconds; default 180), and `maxToolCalls` (1–64; default 12). The wrapper forwards these settings and does not replace the model. Native citation verification and host receipts are preserved in its structured response.

```sh
node src/cli.js ask --request '{"question":"How is recovery verified?","root":"./docs"}' --json
node src/cli.js summarize --request '{"nodeId":"DOCUMENT_ID:NODE_ID","root":"./docs"}' --json
```

Use `--format json`, `--format jsonl`, `--format toon`, `--format yaml`, or `--format md` for incur presentation. `--full-output` requests its envelope. The native response is preserved as structured data; the wrapper does not infer an answer, alter rankings, or claim that a fallback used an index.

Native invocations use argument arrays, never a shell. Standard error is preserved and output capture is bounded at 8 MiB. Ordinary invocations time out after five minutes; host commands allow their requested native timeout plus ten seconds for child cleanup. Use the native CLI for larger outputs or longer indexing runs. Interrupt and termination signals are forwarded to the child. Exit 1 is treated as a no-match success only for search with a recognizable empty `hits` or `results` array and no error marker.

MCP entry points are rejected before incur handles argv. The wrapper does not expose `fetch`, register MCP, sync generated skills, or perform wrapper updates. Incur's standalone binary builder embeds Bun; it does not build or bundle the native Rust executable. Native release packaging is managed by the repository's Rust workflow.

Run `pnpm test` for wrapper parsing, native forwarding, error and no-MCP tests. These tests use an explicit local fixture executable; combined native retrieval tests belong to the Rust workspace and a real-binary smoke test.
