# Actual PageIndex baseline adapter

This research runner executes pinned upstream PageIndex code. It is separate from `scripts/pageindex_eval.py`, which measures GPTgrep against an upstream corpus subset without running PageIndex.

Read [the paired protocol](../../docs/research/paired-baseline-plan.md) before interpreting results.

## Setup

Use an existing Python3.13 interpreter to create a task-local environment, then install `requirements.lock`. No global toolchain change is needed:

```sh
uv venv --python /path/to/existing/python3.13 .local/pageindex-baseline/venv
UV_CACHE_DIR=.local/pageindex-baseline/cache uv pip install \
  --python .local/pageindex-baseline/venv/bin/python \
  -r scripts/pageindex_baseline/requirements.lock
```

Provide read-only checkouts at the exact revisions in `sources.lock.json`. For judging, place the three locked MMLongBench-Doc-V2 files (`eval/judge.py`, `eval/metrics.py`, `LICENSE.md`) in a private source directory at the pinned commit. The runner reads the original judge constants through AST literals; it does not invoke the upstream key-based client or its error-to-false retry behavior. Source code and PDFs are not vendored into this package.

Every runtime verifies source files, all34 PDF hashes, question/metadata hashes, and exact dependency versions before importing PageIndex. Upstream dotenv loading and bytecode writes are disabled. API-key fallbacks are removed from the child environment.

## Stages

Before live answering, prepare and run `capability_probe.py` against the synthetic two-page fixture. Its `--stage prepare` default makes no model calls. The root-selected live `--stage run --max-model-calls 6` exercises the original SDK tool loop using the corrected internal action protocol and records a schema-validated native decision/result roundtrip. It must read page 2 and recover the source fact without receiving that fact in the initial request.

Pass the resulting private `capability.json` to `run.py --capability-receipt`. The receipt must match the current host binary, adapter files, model/effort/profile and input bound. There is no fallback from a missing or failed probe to a scored benchmark run. Indexing itself does not require tool-capability admission.

All invocations require explicit source paths, a private run directory and the selected Codex account home. Use `--rows all` for the complete62-task scope; use `--rows 19,21` for the first development smoke.

```sh
.local/pageindex-baseline/venv/bin/python -B scripts/pageindex_baseline/run.py \
  --stage plan --variant both --rows 19,21 \
  --upstream /path/to/PageIndex \
  --benchmark /path/to/PageIndex-OSS-Benchmark \
  --judge-source /private/path/to/judge-source \
  --run-dir .local/runs/paired-smoke \
  --codex-home /path/to/codex-home
```

- `plan`: verifies and records identities; no model calls.
- `index --variant raw`: actual Flash structure extraction with summaries/optimization disabled, through the SDK's local storage contract; no model calls.
- `index --variant full`: original SDK Flash index, including full optimization, summaries and document description, using the supported custom provider.
- `answer`: original SDK `chat(protocol="responses")`, instructions, tool schemas and Agents Runner, through the injected local Responses transport.
- `judge`: original judge prompt/schema and response-character cap, using the selected local completion profile.
- `run`: index, answer and judge for the selected variants.
- `--variant gptgrep-tree`: common-runtime control using actual GPTgrep parser/tree output but the same SDK reading text, tools, runner and completion transport. It excludes GPTgrep regex/lexical/Jev retrieval.

Model stages require a positive `--max-model-calls` value chosen for the run. The counter bounds local host invocations, not an unobservable count of provider-internal requests. Its default is0. The host uses `gptgrep host-complete`, preserves exact requested model/effort, and validates actual identity and output schema. The input default is256KiB and hard maximum1MiB; oversized content is rejected without truncation.

The raw/full pair shares a private run ledger. Completed caches bind source, adapter, dependency, model/effort, input-cap and binary hashes. Failed attempts are preserved and require a new run directory. Filesystem locking prevents two runners from mutating one run directory simultaneously.

## Interpretation

The transport represents tool decisions as schema JSON returned by local Codex, then lets the real SDK executor run the original tools. That preserves the original tool declarations as data and the SDK execution loop, but is an explicit model/tool-interface adaptation. It is not a native-tool parity claim. Every completion currently creates a fresh host thread, so native GPTgrep versus adapted SDK wall time includes architecture differences. Use the common-runtime tree control to isolate that confound.

Protocol v3 explicitly distinguishes internal `tool_calls` data from native execution and from user-facing `text`. The original PageIndex instructions remain byte-for-byte within the wrapper; original, adapter and combined instruction hashes are separate. Capability qualification is an adapter-level prerequisite. After it passes, every question remains in the QA denominator, including abstentions and zero-page answers. Correct answers from supplied descriptions or summaries retain judge credit while page recall stays zero. Evidence delivery is labelled `raw_pages`, `index_summary_or_metadata`, or `none`; it never automatically verifies a citation. Missing judge verdicts remain unknown and block eligible comparison scores.

Private artifacts include original source-bound trees, response transcripts, request/response hashes, exact native host receipts and phase counts. Terminal output is bounded status metadata. SDK aggregate usage is not authoritative when the transport has no equivalent complete usage object; actual host usage remains in `host-calls.jsonl`. Account-backed monetary billing is unavailable.

Successful page access is counted from tool results, not requested page numbers. Judge failures remain unavailable. A completed raw index or a passing adapter test is not a completed paired baseline, and a paired baseline is not by itself a GPTgrep win.

## Adapter checks

```sh
.local/pageindex-baseline/venv/bin/python -B -m unittest discover \
  -s scripts/pageindex_baseline -p 'test_*.py'
```

The tests use synthetic model decisions and a real local tool invocation through the actual Agents SDK to verify plumbing. They make zero live model calls and produce no benchmark score. Actual model-backed index/answer/judge results must come from separately bounded runs.
