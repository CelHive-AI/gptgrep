# Actual PageIndex baseline adapter

This research runner executes pinned upstream PageIndex code. It is separate from `scripts/pageindex_eval.py`, which measures GPTgrep against an upstream corpus subset without running PageIndex.

Maintainer research notes are local-only. The executable profiles, source locks, receipts and interpretation rules below are the public contract.

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

## Role profiles

`--profile matched-luna-max` is the primary matched-reader configuration: index Luna/max (explicit backend adaptation), reader Luna/max, shared judge Luna/high. `source-default-chat` uses reader Luna/high and judge Luna/high while retaining the explicitly adapted index Luna/max. It is a secondary source-default-chat replay, not a historical reproduction. `legacy-luna-max-control` preserves the earlier all-max control as a separately named configuration.

Existing `--model` and `--reasoning-effort` select the reader. `--index-model` / `--index-reasoning-effort` select indexing; `--judge-model` / `--judge-reasoning-effort` select judging. The upstream index effort is recorded as unspecified, separately from the actual executed max default. All roles share one invocation cap and append phase/model/effort receipts. Capability admission binds the reader profile; changing the judge does not claim a new reader capability.

The declared development membership comes only from the provenance reference manifest; there are no task-row lists in executable code. `--rows all` is the default. Explicit `--rows dev8` loads that existing development split; the remaining rows remain held out. Questions, gold answers and annotations are loaded at runtime. Only the original question and protocol-provided document scope enter the reader; gold/annotations stay with the judge and scorer.

## Stages

Run the live full benchmark directly. Qualification comes from an actual original SDK page-tool roundtrip within that run: a native model selects the declared operation, the SDK returns source-verified page text, and a subsequent completed native turn receives that exact output. Native model/effort, session identity, request/response hashes and source extraction hashes bind the proof. Process completion or an answer string alone does not qualify the transport. No additional live synthetic probe is required or recommended.

Every selected task remains in the denominator, including failures and zero-page answers before the first successful roundtrip. If the full run never demonstrates that ability, the baseline is inadmissible; raw judge results remain visible. Historical capability receipts may be recorded as supporting evidence with `--capability-receipt`, but never qualify a changed run or replace actual benchmark evidence.

All invocations require explicit source paths, a private run directory and the selected Codex account home. The acceptance run uses `--rows all` for the complete62-task,34-document scope.

```sh
.local/pageindex-baseline/venv/bin/python -B scripts/pageindex_baseline/run.py \
  --stage plan --variant full --rows all --profile matched-luna-max \
  --upstream /path/to/PageIndex \
  --benchmark /path/to/PageIndex-OSS-Benchmark \
  --judge-source /private/path/to/judge-source \
  --run-dir .local/runs/full-benchmark \
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

The native product comparator is `scripts/gptgrep_system_eval.py`: it builds an actual PDF-only private corpus using native LiteParse/Rust trees/tgrep, then invokes native `ask --document` with mandatory Jev routing/reranking and the same reader/judge roles. Its build is explicitly deterministic; it does not invent a Jev indexing stage. It preserves per-case raw output, provider failures, partial Jev accounting, source/citation byte integrity, page recall, tool signals and latency including Jev. Plans make no model calls; live runs require an explicit cap and a fresh private directory. The optional baseline summary is loaded only after native answering/judging completes.

First-release acceptance requires a verified live advantage under the declared full-cohort comparison contract. A successful process, capability probe, control smoke or development-only score cannot authorize a first experimental release. No evaluator emits an automatic universal-winner verdict.

Index, answer and judge stages share a private run ledger and cumulative invocation cap. Completed caches bind source, adapter, dependency, all role profiles, bounds and binary hashes. A later stage can increase the cumulative cap without erasing previous calls. Filesystem locking prevents two runners from mutating one run directory simultaneously. Freeze the adapter files and binary before the first live stage.

Use the same arguments and run directory with `--stage index`, then `--stage answer`, then `--stage judge`, or use `--stage run` for all three. A budget-deferred item resumes without redoing completed work. `--retry-failed` explicitly retries failed/interrupted items, retaining each prior attempt under `attempts/` and every earlier host call in the ledger. Each invocation has a durable start journal; an interruption without a final receipt consumes its ordinal and budget conservatively, with unknown usage. Failed attempts are never erased or silently treated as free. Summary files are current projections; immutable stage plans, attempt files and raw call files preserve the full history.

## Interpretation

The transport represents tool decisions as schema JSON returned by local Codex, then lets the real SDK executor run the original tools. That preserves the original tool declarations as data and the SDK execution loop, but is an explicit model/tool-interface adaptation. It is not a native-tool parity claim. Every completion currently creates a fresh host thread, so native GPTgrep versus adapted SDK wall time includes architecture differences. Use the common-runtime tree control to isolate that confound.

Protocol v3 explicitly distinguishes internal `tool_calls` data from native execution and from user-facing `text`. The original PageIndex instructions remain byte-for-byte within the wrapper; original, adapter and combined instruction hashes are separate. The benchmark-native qualification is global to a variant and does not depend on answer correctness or gold evidence pages. Every question remains in the QA denominator, including abstentions and zero-page answers. Correct answers from supplied descriptions or summaries retain judge credit while page recall stays zero. Evidence delivery is labelled `raw_pages`, `index_summary_or_metadata`, or `none`; it never automatically verifies a citation. Missing judge verdicts remain unknown and block eligible comparison scores.

Private artifacts include original source-bound trees, response transcripts, request/response hashes, exact native host receipts and phase counts. Terminal output is bounded status metadata. SDK aggregate usage is not authoritative when the transport has no equivalent complete usage object; actual host usage remains in `host-calls.jsonl`. Account-backed monetary billing is unavailable.

Successful page access is counted from tool results, not requested page numbers. Judge failures remain unavailable. A completed raw index or a passing adapter test is not a completed paired baseline, and a paired baseline is not by itself a GPTgrep win.

## Adapter checks

```sh
.local/pageindex-baseline/venv/bin/python -B -m unittest discover \
  -s scripts/pageindex_baseline -p 'test_*.py'
```

The tests use synthetic model decisions and a real local tool invocation through the actual Agents SDK to verify plumbing. They make zero live model calls and produce no benchmark score. Actual model-backed index/answer/judge results must come from separately bounded runs.
